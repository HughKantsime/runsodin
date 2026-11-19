"""Request cost contracts preserve transaction freshness and refusal bytes."""
import asyncio
import json

import pytest
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.audit_actor import RequestAuditScope
from core.controlled_cache import delivery_cache, request_cache
from core.controlled_parts import any_controlled_data, any_read_restrictions, policies
from core.middleware.controlled_response import ControlledResponseMiddleware
from core.middleware.license_principal import LicensePrincipalMiddleware
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_request_fast_path import statements
from modules.organizations.routes_controlled_parts import change_mark
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_policy_b1 import auth_client, policy_db, PASSWORD


@pytest.mark.parametrize('controlled', [False, True])
@pytest.mark.parametrize('operation', ['create', 'approve'])
def test_controlled_data_is_asked_once_per_request(tmp_path, controlled, operation):
    from tests.test_contracts._controlled_http_probe import run_probe
    result = run_probe('request-policy-cache', r'''
install_license('enterprise', features=['controlled_parts', 'job_approval'])
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Tenant',1)")
uid=seed_user('operator','operator',1)
seed_user('super','admin')
operator=login('operator'); admin=login('super')
sql("INSERT INTO system_config(key,value) VALUES('require_job_approval','true')")
sql("INSERT INTO jobs(id,item_name,status,submitted_by,charged_to_org_id) VALUES(1,'Private','submitted',:uid,1)",uid=uid)
if CONTROLLED:
 sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')")
 sql("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)",uid=uid)
 assert operator.put('/api/controlled-parts/jobs/1',json=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='Test')).status_code==200
 # Approver must have access; redaction is tested with the outside administrator below.
 approver=operator
else:
 approver=admin
from sqlalchemy import event
from core.audit_actor import _BINDING
reads=[]
def record(conn,cursor,statement,*args):
 if statement == 'SELECT 1 FROM controlled_files UNION ALL SELECT 1 FROM controlled_jobs UNION ALL SELECT 1 FROM controlled_job_tombstones LIMIT 1':
  import inspect
  frame=inspect.currentframe()
  while frame and frame.f_code.co_name!='any_controlled_data': frame=frame.f_back
  db=frame.f_locals['db']
  reads.append(bool(db.info.get(_BINDING)))
event.listen(engine,'before_cursor_execute',record)
try:
 response=(operator.post('/api/jobs',json={'item_name':'New','duration_hours':0.25}) if OPERATION=='create'
           else approver.post('/api/jobs/1/approve'))
 assert response.status_code==(201 if OPERATION=='create' else 200),response.text
 assert reads.count(True)==1,reads
 # Staging, external projection and mobile delivery each own a fresh policy boundary.
 if not CONTROLLED: assert reads.count(False)==3,reads
finally:
 event.remove(engine,'before_cursor_execute',record)
if CONTROLLED:
 from fastapi import APIRouter
 router=APIRouter()
 @router.get('/api/jobs/cost-projection')
 def projection(): return [{'id':1,'item_name':'Private'}]
 app.router.routes[0:0]=router.routes
 assert admin.get('/api/jobs/cost-projection').content==b'[{"id":1,"item_name":"[controlled]"}]'
OUT['checked']=True
'''.replace('CONTROLLED', repr(controlled)).replace('OPERATION', repr(operation)), tmp_path, frontend_dist=False)
    assert result['checked'] and result['_net_attempts'] == []


@pytest.mark.parametrize('kind', ['files', 'jobs'])
def test_marking_in_a_request_is_seen_later_in_the_same_request(database, tier_license, kind):
    from core.controlled_redaction import denied_ids
    tier_license('enterprise')
    scope = RequestAuditScope(None)
    scope.register(database)
    try:
        assert not any_read_restrictions(database)
        assert denied_ids(database, actor(2))['job'] == set()
        change_mark(database, kind, 1, mark(), actor())
        assert any_read_restrictions(database)
        assert 1 in denied_ids(database, actor(2))['job']
        change_mark(database, kind, 1, mark(controlled=False), actor())
        assert not any_controlled_data(database)
        assert denied_ids(database, actor(2))['job'] == set()
    finally:
        scope.clear()


def test_tombstone_write_invalidates_history_and_projection(database, tier_license):
    from core.controlled_deletion import redact_job_history_ids
    from core.controlled_parts import tombstone_job_ids
    tier_license('enterprise')
    change_mark(database, 'jobs', 1, mark(), actor())
    rules = policies(database, 'job', 1)
    change_mark(database, 'jobs', 1, mark(controlled=False), actor())
    from core.audit import log_audit
    log_audit(database, 'job.created', 'job', 1)
    database.commit()
    scope = RequestAuditScope(None)
    scope.register(database)
    try:
        assert not any_read_restrictions(database)
        assert tombstone_job_ids(database) == set()
        redact_job_history_ids(database, {1: rules})
        assert any_read_restrictions(database)
        assert tombstone_job_ids(database) == {1}
    finally:
        scope.clear()


def test_delivery_cache_rechecks_after_commit_and_cleans_pool_info(database, tier_license):
    tier_license('enterprise')
    engine = database.get_bind()
    with engine.begin() as connection:
        with delivery_cache(connection), statements(database) as queries:
            assert not any_controlled_data(connection)
            assert not any_read_restrictions(connection)
        assert len(queries) == 1
        assert request_cache(connection) is None
    change_mark(database, 'jobs', 1, mark(), actor())
    with engine.begin() as connection:
        with delivery_cache(connection):
            assert any_controlled_data(connection)
        with pytest.raises(ValueError), delivery_cache(connection):
            raise ValueError('owned delivery failed')
        assert request_cache(connection) is None


def response_messages():
    return [
        {'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'application/json')]},
        {'type': 'http.response.body', 'body': b'[{"id":1,"item_name":"Secret"}]'},
    ]


def run_response():
    sent = []
    async def downstream(scope, receive, send):
        for message in response_messages():
            await send(message)
    async def send(message):
        sent.append(message)
    asyncio.run(ControlledResponseMiddleware(downstream)(
        {'type': 'http', 'method': 'GET', 'path': '/api/jobs/projection', 'headers': []}, None, send))
    return sent


@pytest.mark.parametrize('restricted', [False, True])
@pytest.mark.parametrize('refused', [False, True])
def test_response_resolves_caller_before_restrictions(database, tier_license, monkeypatch, restricted, refused):
    import core.db
    import core.dependencies
    import core.controlled_parts
    tier_license('enterprise')
    if restricted:
        change_mark(database, 'files', 1, mark(), actor())
    monkeypatch.setattr(core.db, 'SessionLocal', sessionmaker(bind=database.get_bind()))
    calls = []
    original = core.controlled_parts.any_read_restrictions
    async def principal(*args):
        calls.append('principal')
        if refused:
            raise HTTPException(403, 'scope refused')
        return actor(2)
    def restrictions(db):
        assert calls == ['principal']
        calls.append('restrictions')
        return original(db)
    monkeypatch.setattr(core.dependencies, '_resolve_current_user', principal)
    monkeypatch.setattr(core.controlled_parts, 'any_read_restrictions', restrictions)
    actual = run_response()
    if refused:
        assert calls == ['principal']
        assert actual[0]['status'] == 403
        assert json.loads(actual[1]['body']) == {'detail': 'scope refused'}
    else:
        assert calls == ['principal', 'restrictions']
        assert actual[0]['status'] == 200
        assert json.loads(actual[1]['body']) == [{'id': 1, 'item_name': '[controlled]' if restricted else 'Secret'}]


def gate():
    from core.app import _register_http_middleware
    app = FastAPI()
    _register_http_middleware(app)
    entry = next(m for m in app.user_middleware if m.cls is LicensePrincipalMiddleware)
    return entry.kwargs


def test_license_gate_disabled_adds_no_task_group(monkeypatch):
    import anyio
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', raising=False)
    monkeypatch.setattr(anyio, 'create_task_group', lambda: pytest.fail('gate created a task group'))
    calls = []
    async def downstream(scope, receive, send):
        calls.append((scope, receive, send, asyncio.current_task()))
    async def run():
        scope = {'type': 'http'}
        await LicensePrincipalMiddleware(downstream, **gate())(scope, None, None)
        assert calls == [(scope, None, None, asyncio.current_task())]
    asyncio.run(run())


PUBLIC = [('OPTIONS', '/api/private'), ('GET', '/dashboard'), ('HEAD', '/dashboard'),
          ('GET', '/api/license'), ('HEAD', '/api/license'), ('POST', '/api/health'),
          ('POST', '/api/setup'), ('POST', '/api/auth/login'), ('GET', '/api/health/live'),
          ('POST', '/api/setup/finish'), ('GET', '/api/static/a'), ('POST', '/static/a'),
          ('POST', '/assets/a'), ('GET', '/api/branding')]
PRIVATE = [('POST', '/dashboard'), ('POST', '/api/license'), ('HEAD', '/api/branding'),
           ('GET', '/api/healthfoo'), ('GET', '/api/setupfoo'), ('GET', '/api/private'),
           ('GET', '/api'), ('GET', '/static'), ('GET', '/ws'), ('GET', '/openapi.json')]


@pytest.mark.parametrize('version', [False, True])
@pytest.mark.parametrize('method,path,public', [(m, p, True) for m, p in PUBLIC] + [(m, p, False) for m, p in PRIVATE])
@pytest.mark.parametrize('credential', ['cookie', 'bearer', 'query', 'key', 'basic', 'blank', 'mixed'])
def test_license_gate_enabled_refuses_exactly_as_before(monkeypatch, version, method, path, public, credential):
    from contextlib import nullcontext
    import core.db
    import core.dependencies
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', '1')
    monkeypatch.setattr(core.db, 'SessionLocal', lambda: nullcontext(None))
    calls, sent = [], []
    def refuse(*args, **kwargs):
        assert kwargs == {'side_effects': False}
        calls.append(True)
        raise HTTPException(403, {'code': 'license_user_locked'}, headers={'X-Test': 'refused'})
    monkeypatch.setattr(core.dependencies, 'resolve_current_user_sync', refuse)
    async def downstream(scope, receive, send):
        sent.append('passed')
    async def send(message):
        sent.append(message)
    headers = {'cookie': [(b'cookie', b'session=probe')], 'bearer': [(b'authorization', b'bEaReR probe')],
               'key': [(b'x-api-key', b'probe')], 'query': [], 'basic': [(b'authorization', b'Basic probe')],
               'blank': [(b'authorization', b'Bearer   ')],
               'mixed': [(b'cookie', b'session=probe'), (b'x-api-key', b'probe')]}
    scope = {'type': 'http', 'method': method, 'path': path.replace('/api/', '/api/v1/') if version else path,
             'headers': headers[credential], 'query_string': b'token=probe'}
    asyncio.run(LicensePrincipalMiddleware(downstream, **gate())(scope, None, send))
    refused = not public and credential in {'cookie', 'bearer', 'mixed'}
    assert len(calls) == int(refused)
    if refused:
        assert sent[0]['status'] == 403
        assert (b'x-test', b'refused') in sent[0]['headers']
        assert sent[1]['body'] == b'{"detail":{"code":"license_user_locked"}}'
    else:
        assert sent == ['passed']


def test_gate_runtime_toggle_outage_and_non_http(monkeypatch):
    import core.db
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', raising=False)
    def unavailable():
        raise RuntimeError('unavailable')
    monkeypatch.setattr(core.db, 'SessionLocal', unavailable)
    sent = []
    async def downstream(*args):
        sent.append('passed')
    async def send(message):
        sent.append(message)
    middleware = LicensePrincipalMiddleware(downstream, **gate())
    scope = {'type': 'http', 'method': 'GET', 'path': '/api/private', 'headers': [(b'cookie', b'session=probe')]}
    asyncio.run(middleware(scope, None, send))
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', '1')
    asyncio.run(middleware(scope, None, send))
    asyncio.run(middleware({'type': 'websocket'}, None, send))
    assert sent[0] == sent[-1] == 'passed'
    assert sent[1]['status'] == 503
    assert sent[2]['body'] == b'{"detail":"Service temporarily unavailable"}'


def test_middleware_order_enforces_network_policy_before_principal_resolution():
    from core.app import create_app
    app = create_app()
    names = [m.kwargs['dispatch'].__name__ if 'dispatch' in m.kwargs else m.cls.__name__
             for m in app.user_middleware]
    assert names == [
        'TrustedProxySchemeMiddleware', 'security_headers', 'CORSMiddleware', 'check_ip_allowlist',
        'AuditActorMiddleware', 'ControlledResponseMiddleware', 'LicensePrincipalMiddleware',
        'authenticate_request', 'idempotency_middleware', 'dry_run_middleware', 'TrustedHostMiddleware',
    ]
    assert names.count('LicensePrincipalMiddleware') == 1
    assert 'license_principal_gate' not in names


@pytest.mark.parametrize('previous_flag', ['absent', False, True])
@pytest.mark.parametrize('failed', [False, True])
def test_delivery_discards_existing_answers_and_restores_only_flag(database, previous_flag, failed):
    from contextlib import nullcontext
    with database.get_bind().connect() as connection:
        if previous_flag != 'absent':
            connection.info['_controlled_request'] = previous_flag
        connection.info['_controlled_present'] = False
        connection.info[('_controlled_ids', 1)] = {'job': set()}
        connection.info['unrelated'] = 'preserved'
        with pytest.raises(ValueError) if failed else nullcontext():
            with delivery_cache(connection):
                assert '_controlled_present' not in connection.info
                assert ('_controlled_ids', 1) not in connection.info
                assert not any_controlled_data(connection)
                connection.execute(text("INSERT INTO controlled_jobs(job_id,audience_id,label,printer_tag,retention_days,reason,marked_at) "
                                        "VALUES(1,1,'CUI','Secure',7,'Regression test',CURRENT_TIMESTAMP)"))
                connection.commit()
                if failed:
                    raise ValueError('delivery failed after commit')
        assert '_controlled_present' not in connection.info
        assert ('_controlled_ids', 1) not in connection.info
        assert connection.info.get('_controlled_request', 'absent') == previous_flag
        assert connection.info['unrelated'] == 'preserved'
        assert any_controlled_data(connection)
        # Do not leave test-owned opt-in state on the pool.
        connection.info.clear()


@pytest.mark.parametrize('binding', ['http', 'owned'])
@pytest.mark.parametrize('writer', ['revoke', 'grant', 'erase'])
def test_membership_writers_invalidate_denied_projection(database, tier_license, binding, writer):
    from core.controlled_redaction import denied_ids
    from modules.organizations.routes_controlled_audiences import AudienceRequest, save
    from modules.organizations.gdpr_erasure import erase_user_data_sync
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    scope = RequestAuditScope(None)
    if binding == 'http':
        scope.register(database)
    else:
        database.info['_controlled_request'] = True
    subject = actor(2) if writer == 'grant' else actor()
    try:
        assert (1 in denied_ids(database, subject)['job']) == (writer == 'grant')
        if writer == 'erase':
            erase_user_data_sync(1, actor(2), database)
        else:
            save(database, AudienceRequest(name='Secure', user_ids=[1, 2] if writer == 'grant' else []), actor(2), 1)
        assert (1 in denied_ids(database, subject)['job']) == (writer != 'grant')
    finally:
        scope.clear()
        database.info.clear()


@pytest.fixture
def enabled_gate_client(auth_client):
    auth_client.app.add_middleware(LicensePrincipalMiddleware, **gate())
    response = auth_client.post('/api/v1/auth/login', data={'username': 'admin', 'password': PASSWORD})
    assert response.status_code == 200
    assert auth_client.cookies.get('session')
    return auth_client


def test_enabled_gate_preserves_full_json_body_for_valid_session(enabled_gate_client):
    @enabled_gate_client.app.post('/api/v1/body-probe')
    async def body_probe(request: Request):
        assert request.state.resolved_license_principal['id'] == 3
        return {'body': (await request.body()).decode(), 'json': await request.json()}
    body = '{"message": "complete body", "nested": {"items": [1, 2, 3]}}'
    response = enabled_gate_client.post('/api/v1/body-probe', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 200
    assert response.json() == {'body': body, 'json': json.loads(body)}


def test_enabled_gate_exposes_principal_to_downstream_dependency(enabled_gate_client):
    def principal(request: Request):
        return request.state.resolved_license_principal
    @enabled_gate_client.app.get('/api/v1/principal-probe')
    def principal_probe(user=Depends(principal)):
        return {'id': user['id'], 'auth_kind': user['_auth_kind']}
    response = enabled_gate_client.get('/api/v1/principal-probe')
    assert response.status_code == 200
    assert response.json() == {'id': 3, 'auth_kind': 'session_jwt'}
