"""Empty installs avoid policy fan-out; populated installs retain every guard."""
import asyncio
import json
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.audit_actor import RequestAuditScope
from core.controlled_parts import any_controlled_data, any_marks, any_read_restrictions, policies
from core.controlled_request import authorize_references
from core.middleware.controlled_response import ControlledResponseMiddleware
from core.recipient_policy import resolve_alert_scope
from tests.test_contracts.test_controlled_parts import database, actor, mark
from modules.organizations.routes_controlled_parts import change_mark


@contextmanager
def statements(db):
    queries = []
    def record(conn, cursor, statement, *args):
        queries.append(statement)
    event.listen(db.get_bind(), 'before_cursor_execute', record)
    try:
        yield queries
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', record)


def test_one_empty_lookup_per_request_no_body_or_reference_walk(database, monkeypatch):
    import core.dependencies as dependencies
    import core.controlled_request as guards
    async def principal(*args):
        return actor()
    async def unexpected_body(*args):
        pytest.fail('empty install parsed the request body')
    def unexpected_access(*args):
        pytest.fail('empty install authorized an individual reference')
    monkeypatch.setattr(dependencies, '_resolve_current_user', principal)
    monkeypatch.setattr(Request, 'json', unexpected_body)
    monkeypatch.setattr(guards, 'assert_access', unexpected_access)
    request = Request({'type':'http', 'method':'POST', 'path':'/api/jobs', 'headers':[]})
    for _ in range(2):
        scope = RequestAuditScope('127.0.0.1')
        scope.register(database)
        try:
            with statements(database) as queries:
                assert asyncio.run(dependencies.get_current_user(request, None, database)) == actor()
                assert not any_marks(database)
                assert not any_read_restrictions(database)
                authorize_references(database, actor(), {'jobs':[{'model_id':1}] * 100})
                for identifier in range(100):
                    assert policies(database, 'job', identifier) == []
            assert len(queries) == 1
        finally:
            scope.clear()


@pytest.mark.parametrize('kind', ['files', 'jobs', 'history'])
def test_first_controlled_row_reenables_checks_next_request(database, tier_license, kind):
    tier_license('enterprise')
    scope = RequestAuditScope('127.0.0.1')
    scope.register(database)
    assert not any_controlled_data(database)
    scope.clear()
    if kind == 'history':
        database.execute(text("INSERT INTO controlled_job_tombstones(job_id,audience_id,label,printer_tag,retention_days,"
                              "marked_at,created_at,audit_max_id,rule_index) "
                              "VALUES(99,1,'CUI','Secure',7,'2020-01-01','2020-01-01',0,0)"))
        database.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) "
                              "VALUES(99,1,99,'Secret','completed',CURRENT_TIMESTAMP)"))
        database.commit()
    else:
        change_mark(database, kind, 1, mark(), actor())
    scope = RequestAuditScope('127.0.0.1')
    scope.register(database)
    try:
        assert any_controlled_data(database)
        assert any_read_restrictions(database)
        with pytest.raises(HTTPException) as exc:
            authorize_references(database, actor(2), {'nested':{'job_ids':[99 if kind == 'history' else 1]}})
        assert exc.value.detail['code'] == 'controlled_part_group_required'
    finally:
        scope.clear()


@pytest.mark.parametrize('binding', ['http', 'owned'])
def test_mark_invalidates_request_cache_and_membership_is_never_cached(database, tier_license, binding):
    tier_license('enterprise')
    scope = RequestAuditScope('127.0.0.1')
    if binding == 'http':
        scope.register(database)
    else:
        database.info['_controlled_request'] = True
    try:
        assert not any_controlled_data(database)
        change_mark(database, 'files', 1, mark(), actor())
        if binding == 'owned':
            assert database.info['_controlled_request'] is True
        assert any_controlled_data(database)
        authorize_references(database, actor(), {'file_id':1})
        database.execute(text('DELETE FROM controlled_audience_members WHERE user_id=1'))
        database.commit()
        with pytest.raises(HTTPException):
            authorize_references(database, actor(), {'file_id':1})
    finally:
        scope.clear()


def test_recipient_lookup_is_constant_and_rechecks_each_delivery(database, tier_license):
    tier_license('enterprise')
    # No HTTP binding: these are separate post-commit delivery invocations.
    for identifier in range(3, 43):
        database.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                              "VALUES(:id,:name,'unused','admin',1)"), {'id':identifier,'name':str(identifier)})
    database.commit()
    with statements(database) as queries:
        assert resolve_alert_scope(database, job_id=1)[0] == set(range(1, 43))
    assert len(queries) == 3  # owner, recipients, one controlled-state lookup
    change_mark(database, 'files', 1, mark(), actor())
    assert resolve_alert_scope(database, job_id=1)[0] == {1}
    database.execute(text('DELETE FROM controlled_audience_members WHERE user_id=1'))
    database.commit()
    assert resolve_alert_scope(database, job_id=1)[0] == set()


@pytest.mark.parametrize('controlled', [False, True])
def test_asgi_stream_passthrough_or_redaction(database, tier_license, monkeypatch, controlled):
    import core.db
    tier_license('enterprise')
    if controlled:
        change_mark(database, 'files', 1, mark(), actor())
    monkeypatch.setattr(core.db, 'SessionLocal', sessionmaker(bind=database.get_bind()))
    content = json.dumps([{'id':1,'item_name':'Secret'}, {'id':2,'item_name':'Public'}]).encode()
    messages = [
        {'type':'http.response.start','status':200,'headers':[
            (b'content-type', b'application/json'), (b'content-length', str(len(content)).encode()),
            (b'set-cookie', b'one=1'), (b'set-cookie', b'two=2')]},
        {'type':'http.response.body','body':content[:20],'more_body':True},
        {'type':'http.response.body','body':content[20:],'more_body':False},
    ]
    sent, background = [], []
    async def downstream(scope, receive, send):
        for message in messages:
            await send(message)
            if not controlled:
                assert sent[-1] is message  # forwarded immediately, no body copy
        background.append(True)
    async def send(message): sent.append(message)
    scope = {'type':'http','method':'GET','path':'/api/jobs','headers':[],
             'state':{'controlled_actor':actor(2),'controlled_marks':controlled}}
    with statements(database) as queries:
        asyncio.run(ControlledResponseMiddleware(downstream)(scope, None, send))
    assert background == [True]
    assert [v for k,v in sent[0]['headers'] if k == b'set-cookie'] == [b'one=1', b'two=2']
    body = b''.join(m['body'] for m in sent[1:])
    assert int(dict(sent[0]['headers'])[b'content-length']) == len(body)
    assert json.loads(body) == [{'id':1,'item_name':'[controlled]' if controlled else 'Secret'}, {'id':2,'item_name':'Public'}]
    if not controlled:
        assert queries == []


def test_asgi_replay_auth_refusal_suppresses_original_body(database, monkeypatch):
    import core.db
    import core.dependencies
    monkeypatch.setattr(core.db, 'SessionLocal', sessionmaker(bind=database.get_bind()))
    async def refuse(*args):
        raise HTTPException(403, 'scope refused')
    monkeypatch.setattr(core.dependencies, '_resolve_current_user', refuse)
    sent = []
    async def downstream(scope, receive, send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json')]})
        await send({'type':'http.response.body','body':b'{"secret":"cached"}'})
    async def send(message): sent.append(message)
    scope = {'type':'http','method':'POST','path':'/api/jobs','headers':[]}
    asyncio.run(ControlledResponseMiddleware(downstream)(scope, None, send))
    assert sent[0]['status'] == 403
    assert json.loads(sent[1]['body']) == {'detail':'scope refused'}
    assert len(sent) == 2


def test_real_http_empty_state_budgets_and_auth_modes(tmp_path):
    from tests.test_contracts._controlled_http_probe import run_probe
    result = run_probe('controlled-fast-path-http', r'''
install_license('enterprise', features=['controlled_parts', 'job_approval'])
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Load tenant',1)")
uid=seed_user('viewer','viewer',1)
sid=seed_user('super','admin')
viewer=login('viewer'); admin=login('super')
sql("INSERT INTO system_config(key,value) VALUES('require_job_approval','true')")
sql("INSERT INTO printers(id,name,tags,org_id,slot_count,is_active,timelapse_enabled,shared) "
    "VALUES(1,'Public printer','[]',1,4,1,0,0)")
sql("INSERT INTO jobs(id,item_name,status,submitted_by,charged_to_org_id) VALUES(1,'Approve me','submitted',:uid,1)", uid=uid)
assert viewer.post('/api/jobs',json={'item_name':'Warmup','duration_hours':0.25}).status_code==201
import core.dependencies as dependencies
from unittest.mock import patch
from sqlalchemy import event
original=dependencies._resolve_current_user
resolutions=[]
async def resolve(*args, **kwargs):
 resolutions.append(True)
 return await original(*args, **kwargs)
queries=[]
def record(conn,cursor,statement,*args):
 queries.append(statement)
event.listen(engine,'before_cursor_execute',record)
with patch.object(dependencies,'_resolve_current_user',resolve):
 for method,path,c,payload,expected,budget in [
  ('POST','/api/jobs',viewer,{'item_name':'Measured','duration_hours':0.25},201,5),
  ('POST','/api/jobs/1/approve',admin,None,200,5),
  ('GET','/api/jobs',viewer,None,200,1),
  ('GET','/api/printers',viewer,None,200,1),
 ]:
  queries.clear(); resolutions.clear()
  response=c.request(method,path,**({'json':payload} if payload else {}))
  assert response.status_code==expected,(path,response.status_code,response.text)
  controlled=[q for q in queries if 'controlled_' in q]
  assert len(controlled)==budget,(path,controlled)
  assert all('SELECT 1 FROM controlled_files UNION ALL SELECT 1 FROM controlled_jobs UNION ALL SELECT 1 FROM controlled_job_tombstones LIMIT 1'==q for q in controlled)
  assert len(resolutions)==1,(path,resolutions)
 # Superadmin dependencies and both API-key paths avoid fallback resolution.
 resolutions.clear()
 assert admin.get('/api/controlled-parts/audiences').status_code==200
 assert len(resolutions)==1
 raw='odin_fast_path_contract_only'
 with engine.begin() as connection:
  connection.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,'probe',:h,:p,:s)"),
                     {'u':sid,'h':hash_password(raw),'p':raw[:10],'s':'["admin"]'})
 os.environ['API_KEY']='fast-path-legacy-contract-only'
 for key in (raw,os.environ['API_KEY']):
  api=client()
  for path in ('/api/jobs','/api/printers'):
   resolutions.clear()
   response=api.get(path,headers={'X-API-Key':key})
   assert response.status_code==200,(path,response.status_code,response.text)
   assert len(resolutions)==1,(path,resolutions)
event.remove(engine,'before_cursor_execute',record)
OUT['checked']=True
''', tmp_path, frontend_dist=False)
    assert result['checked'] and result['_net_attempts'] == []
