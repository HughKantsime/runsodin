"""Real hashed tokens, local SQLite and synthetic Square reads; no network or printers."""
import json
import secrets
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from fastapi import Request
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from core.api_token_policy import SQUARE_SETUP_SCOPE, token_scope_granted
from core.models import AuditLog, SystemConfig
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_square_readonly_workflow import shop, mapped, connect
from tests.test_contracts.test_batch_acceptance import batch
from tests.test_contracts.test_square_delivery import prepared
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers

NOTE = 'Approved by the inventory owner in work order 123 on 2026-11-16'


@pytest.fixture(autouse=True)
def setup_policy(tier_license):
    tier_license('pro')
    from core.rate_limit import limiter
    limiter._storage.reset()


def authenticate(shop, scopes=(SQUARE_SETUP_SCOPE,), role='admin'):
    from core.auth import hash_password
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.audit_actor import register_request_session
    from core.middleware.audit_actor import AuditActorMiddleware
    from modules.organizations.routes_sessions import router
    client, _, _, db, app, *_ = shop
    raw = 'odin_' + secrets.token_urlsafe(32)
    db.execute(text('UPDATE users SET role=:role WHERE id=3'), {'role': role})
    db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'Inventory setup',:hash,:prefix,:scopes)"),
               {'hash': hash_password(raw), 'prefix': raw[:10], 'scopes': json.dumps(list(scopes))})
    db.commit()
    token_id = db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar_one()
    app.include_router(router)
    # shop has already served its session setup requests; install request-local audit binding.
    app.middleware_stack = AuditActorMiddleware(app.middleware_stack)
    def database(request: Request):
        with Session(db.get_bind()) as session:
            register_request_session(session, request)
            yield session
    app.dependency_overrides[get_db] = database
    app.dependency_overrides.pop(get_current_user)
    client.headers['X-API-Key'] = raw
    return token_id


def payload(revision, baseline=False, **extra):
    fields = {'at': datetime.now(timezone.utc).isoformat(), 'quantity': 7} if baseline else {'location_id': 'L1', 'variation_id': 'V1'}
    return {'revision': revision, 'approval_note': NOTE, **fields, **extra}


def path(baseline=False):
    return '/products/101/square' + ('/baseline' if baseline else '')


@pytest.mark.parametrize('scopes', [['write'], ['admin'], ['read'], ['write:production'], ['agent:write']])
@pytest.mark.parametrize('baseline', [False, True])
def test_scope_must_be_explicit_on_both_routes(shop, scopes, baseline):
    status = mapped(shop)
    authenticate(shop, scopes)
    assert shop[0].put(path(baseline), json=payload(status['revision'], baseline)).status_code == 403
    assert not token_scope_granted(scopes, SQUARE_SETUP_SCOPE)


@pytest.mark.parametrize('baseline', [False, True])
def test_delegated_success_has_exact_audit_actor_and_never_constructs_writer(shop, monkeypatch, baseline):
    from modules.orders import square_delivery_client
    status = mapped(shop) if baseline else connect(shop[0])[0]
    token_id = authenticate(shop)
    writer = Mock(side_effect=AssertionError('Square writer must not be constructed'))
    monkeypatch.setattr(square_delivery_client, 'SquareWriter', writer)
    shop[2].clear()
    result = shop[0].put(path(baseline), json=payload(status['revision'], baseline))
    assert result.status_code == 200, result.text
    action = 'square.stock_baseline' if baseline else 'square.product_mapped'
    row = shop[3].execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id.desc())).scalars().first()
    assert row.details.items() >= {'auth_kind': 'user_api_token', 'token_id': token_id,
        'token_name': 'Inventory setup', 'token_owner_user_id': 3, 'approval_note': NOTE}.items()
    actor = shop[3].execute(text('SELECT actor_auth_kind,actor_user_id FROM audit_logs WHERE id=:id'), {'id': row.id}).one()
    assert actor == ('user_api_token', 3)
    writer.assert_not_called()
    assert all(method == 'GET' or route == '/v2/inventory/counts/batch-retrieve' for method, route, *_ in shop[2])


@pytest.mark.parametrize('baseline', [False, True])
@pytest.mark.parametrize('note', [None, '', '   ', 'x' * 501, 123, 'missing'])
def test_note_is_required_and_validated_before_any_square_read(shop, baseline, note):
    status = mapped(shop)
    authenticate(shop)
    body = payload(status['revision'], baseline, approval_note=note)
    if note == 'missing':
        body.pop('approval_note')
    shop[2].clear()
    assert shop[0].put(path(baseline), json=body).status_code == 422
    assert shop[2] == []


@pytest.mark.parametrize('note', ['a', 'x' * 500])
def test_note_length_boundaries(shop, note):
    status = mapped(shop)
    authenticate(shop)
    assert shop[0].put(path(), json=payload(status['revision'], approval_note=note)).status_code == 200


@pytest.mark.parametrize('role', ['operator', 'viewer'])
@pytest.mark.parametrize('baseline', [False, True])
def test_scope_never_raises_role(shop, role, baseline):
    status = mapped(shop)
    authenticate(shop, role=role)
    assert shop[0].put(path(baseline), json=payload(status['revision'], baseline)).status_code == 403


def test_permission_matrix_still_applies(shop):
    status = mapped(shop)
    authenticate(shop)
    shop[3].add(SystemConfig(key='rbac_permissions', value={'action_access': {'products.square_manage': []}}))
    shop[3].commit()
    for baseline in (False, True):
        assert shop[0].put(path(baseline), json=payload(status['revision'], baseline)).status_code == 403


def test_inventory_enumerates_all_square_manage_routes_and_refuses_other_routes(shop):
    from core.rbac_routes import ACTION_ROUTES
    from modules.orders.square_setup_auth import SQUARE_SETUP_ROUTES
    from modules.orders.routes.square import router
    def actions(dep):
        return [getattr(dep.call, '__odin_rbac_action__', (None,))[0]] + [a for child in dep.dependencies for a in actions(child)]
    actual = {(method, '/api' + route.path) for route in router.routes
              if 'products.square_manage' in actions(route.dependant) for method in route.methods}
    assert actual == set(ACTION_ROUTES['products.square_manage'])
    status = mapped(shop)
    authenticate(shop, [SQUARE_SETUP_SCOPE, 'admin', 'write:production'])
    for method, route in actual:
        relative = route.removeprefix('/api')
        if (method, relative) in SQUARE_SETUP_ROUTES:
            continue
        response = shop[0].request(method, relative.replace('{product_id}', '101').replace('{addition_id}', 'synthetic'),
                                   params={'revision': status['revision']}, json={})
        assert response.status_code == 403, (method, route, response.text)


def test_scope_grant_revoke_list_and_immediate_enforcement(shop):
    status = mapped(shop)
    token_id = authenticate(shop, ['read'])
    client = shop[0]
    url = f'/tokens/{token_id}/square-setup'
    assert client.put(path(), json=payload(status['revision'])).status_code == 403
    for enabled in (True, False):
        result = client.put(url, headers=interactive_headers(), json={'enabled': enabled})
        assert result.status_code == 200
        assert (SQUARE_SETUP_SCOPE in result.json()['scopes']) is enabled
        listed = client.get('/tokens', headers=interactive_headers()).json()
        assert (SQUARE_SETUP_SCOPE in next(t for t in listed if t['id'] == token_id)['scopes']) is enabled
        result = client.put(path(), json=payload(status['revision']))
        assert result.status_code == (200 if enabled else 403)
        if enabled:
            status = result.json()
    rows = shop[3].execute(select(AuditLog).where(AuditLog.action.like('api_token.square_setup_%')).order_by(AuditLog.id)).scalars().all()
    assert [r.action for r in rows] == ['api_token.square_setup_granted', 'api_token.square_setup_revoked']
    for row, before, after in zip(rows, [['read'], ['read', SQUARE_SETUP_SCOPE]], [['read', SQUARE_SETUP_SCOPE], ['read']]):
        assert row.details == {'administrator_id': 3, 'token_id': token_id, 'token_name': 'Inventory setup',
                               'token_owner_user_id': 3, 'auth_kind': 'bearer_jwt',
                               'scopes_before': before, 'scopes_after': after}


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('kind', ['token', 'legacy', 'operator', 'viewer', 'unknown'])
def test_grant_and_revoke_require_interactive_admin(shop, enabled, kind):
    from core.dependencies import get_current_user
    mapped(shop)
    token_id = authenticate(shop, ['admin', SQUARE_SETUP_SCOPE])
    if kind != 'token':
        shop[4].dependency_overrides[get_current_user] = lambda: {
            'id': 3, 'role': kind if kind in ('operator', 'viewer') else 'admin',
            '_auth_kind': 'legacy_global_api_key' if kind == 'legacy' else None if kind == 'unknown' else 'bearer_jwt'}
    assert shop[0].put(f'/tokens/{token_id}/square-setup', json={'enabled': enabled}).status_code == 403


@pytest.mark.parametrize('problem', ['education', 'community', 'foreign', 'camera'])
def test_grant_refusals(shop, tier_license, problem):
    mapped(shop)
    token_id = authenticate(shop, ['read:cameras'] if problem == 'camera' else ['read'])
    db = shop[3]
    if problem == 'education':
        db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','true')"))
    if problem == 'community':
        tier_license(None)
    if problem == 'foreign':
        db.execute(text('UPDATE api_tokens SET user_id=5 WHERE id=:id'), {'id': token_id})
    db.commit()
    response = shop[0].put(f'/tokens/{token_id}/square-setup', headers=interactive_headers(), json={'enabled': True})
    assert response.status_code == {'education': 403, 'community': 403, 'foreign': 404, 'camera': 409}[problem]
    assert SQUARE_SETUP_SCOPE not in json.loads(db.execute(text('SELECT scopes FROM api_tokens WHERE id=:id'), {'id': token_id}).scalar_one())


def test_scope_cannot_be_minted_and_upgrade_does_not_grant_it(shop):
    mapped(shop)
    authenticate(shop, ['admin'])
    client = shop[0]
    assert client.post('/tokens', headers=interactive_headers(), json={'name': 'New setup', 'scopes': [SQUARE_SETUP_SCOPE]}).status_code == 403
    result = client.post('/tokens', headers=interactive_headers(), json={'name': 'New token', 'scopes': ['write']})
    assert result.status_code == 200
    assert SQUARE_SETUP_SCOPE not in result.json()['scopes']
    assert all(SQUARE_SETUP_SCOPE not in t['scopes'] for t in client.get('/tokens', headers=interactive_headers()).json())


def test_education_mode_blocks_use_but_allows_revocation(shop):
    status = mapped(shop)
    token_id = authenticate(shop, ['read', SQUARE_SETUP_SCOPE])
    shop[3].execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','true')")); shop[3].commit()
    for baseline in (False, True):
        assert shop[0].put(path(baseline), json=payload(status['revision'], baseline)).status_code == 403
    assert shop[0].put(f'/tokens/{token_id}/square-setup', headers=interactive_headers(), json={'enabled': False}).status_code == 200


def test_sessions_need_no_note_and_baseline_keeps_previous_value(shop):
    status = mapped(shop)
    first = payload(status['revision'], True); first.pop('approval_note')
    response = shop[0].put(path(True), json=first)
    assert response.status_code == 200
    status = response.json()['connection']
    previous = status['mappings'][0]['stock_baseline']
    response = shop[0].put(path(True), json=payload(status['revision'], True, quantity=22))
    assert response.status_code == 200
    row = shop[3].execute(select(AuditLog).where(AuditLog.action == 'square.stock_baseline').order_by(AuditLog.id.desc())).scalars().first()
    assert row.details['previous_baseline'] == previous


def test_existing_session_helper_callers_do_not_need_a_new_field(shop):
    from types import SimpleNamespace
    from modules.orders.stock_baseline import set_baseline
    status = mapped(shop)
    result = set_baseline(shop[3], shop[1], 101,
                          SimpleNamespace(revision=status['revision'], at='2099-01-01T00:00:00Z', quantity=12))
    assert result['connection']['mappings'][0]['stock_baseline']['quantity'] == 12


def test_existing_map_helper_callers_need_no_request(shop):
    from modules.orders.routes import square
    from tests.test_contracts.test_square_readonly_workflow import connect
    status, _ = connect(shop[0])
    result = square.map_product(101, square.Mapping(
        revision=status['revision'], location_id='L1', variation_id='V1'), shop[1], shop[3])
    assert result['mappings'][0]['variation_id'] == 'V1'


@pytest.mark.parametrize('delegated', [False, True])
def test_mapping_audit_keeps_original_session_string_and_readers_accept_both(shop, tier_license, delegated):
    import csv
    import io
    from core.dependencies import get_current_user
    from modules.reporting.routes.exports import router
    tier_license('enterprise')
    status = connect(shop[0])[0] if delegated else mapped(shop)
    expected = 'Explicit Square variation/location selected'
    if delegated:
        token_id = authenticate(shop)
        assert shop[0].put(path(), json=payload(status['revision'])).status_code == 200
        expected = {'description': expected, 'location_id': 'L1', 'variation_id': 'V1',
                    'item_name': 'Part', 'variation_name': 'Regular', 'previous': None,
                    'auth_kind': 'user_api_token', 'token_id': token_id,
                    'token_name': 'Inventory setup', 'token_owner_user_id': 3, 'approval_note': NOTE}
    details = shop[3].execute(text(
        "SELECT details FROM audit_logs WHERE action='square.product_mapped' ORDER BY id DESC LIMIT 1"
    )).scalar_one()
    assert details == json.dumps(expected)
    shop[4].include_router(router)
    shop[4].dependency_overrides[get_current_user] = lambda: {
        'id': 3, 'role': 'admin', 'group_id': None, '_auth_kind': 'bearer_jwt'}
    params = {'action': 'square.product_mapped'}
    listed = shop[0].get('/audit-logs', params=params)
    assert listed.status_code == 200
    assert listed.json()['logs'][0]['details'] == expected
    exported = shop[0].get('/export/audit-logs', params=params)
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert rows[0]['Details'] == (json.dumps(expected) if delegated else expected)


@pytest.mark.parametrize('route', ['grant', 'map', 'baseline'])
def test_stored_education_mode_change_blocks_existing_grant(shop, route):
    status = mapped(shop)
    token_id = authenticate(shop, ['read'])
    client, _, calls, db, *_ = shop
    url = f'/tokens/{token_id}/square-setup'
    db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','false')"))
    db.commit()
    assert client.put(url, headers=interactive_headers(), json={'enabled': True}).status_code == 200
    db.execute(text("UPDATE system_config SET value='true' WHERE key='education_mode'"))
    db.commit()
    calls.clear()
    if route == 'grant':
        result = client.put(url, headers=interactive_headers(), json={'enabled': True})
    else:
        baseline = route == 'baseline'
        result = client.put(path(baseline), json=payload(status['revision'], baseline))
    assert result.status_code == 403
    assert result.json()['detail'] == 'Square setup permission is unavailable in Education mode'
    assert calls == []


@pytest.mark.parametrize('state', ['prepared', 'unknown'])
def test_existing_addition_barriers_preserved_for_tokens(batch, state):
    from modules.orders import batch_production as batches, stock_recovery
    row = prepared(batch)
    if state == 'unknown':
        batch[3].execute(update(batches.additions).values(state='unknown', attempts=1)); batch[3].commit()
    status = batch[0].get('/products/square').json()
    authenticate(batch)
    result = batch[0].put(path(True), json=payload(status['revision'], True))
    assert result.status_code == (200 if state == 'prepared' else 409)
    if state == 'prepared':
        assert result.json()['pending_additions'] == [row['id']]
        assert stock_recovery.check(batch[3], batch[1], row['id'])['message'] == 'baseline_review'


@pytest.mark.parametrize('baseline', [False, True])
def test_stale_revision_and_foreign_products_still_refused(shop, baseline):
    mapped(shop)
    authenticate(shop)
    assert shop[0].put(path(baseline), json=payload('stale', baseline)).status_code == 409
    # Mapping checks ownership before revision; baseline checks the connection first.
    from modules.orders import square_store
    doc, _ = square_store.load(shop[3], 1)
    assert shop[0].put(path(baseline).replace('/101/', '/102/'), json=payload(doc['revision'], baseline)).status_code == 404


def test_untracked_mapping_still_refused(shop, monkeypatch):
    from modules.orders.square_client import SquareReader
    status = connect(shop[0])[0]
    authenticate(shop)
    monkeypatch.setattr(SquareReader, 'variation', lambda *a: {'tracked': False})
    assert shop[0].put(path(), json=payload(status['revision'])).status_code == 409


def test_existing_limiter_is_keyed_by_token_not_client_address(shop):
    from slowapi.errors import RateLimitExceeded
    from slowapi import _rate_limit_exceeded_handler
    from core.rate_limit import limiter
    status = mapped(shop)
    authenticate(shop)
    shop[4].state.limiter = limiter
    shop[4].add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # Rebuild only Starlette's exception middleware; this test checks the 429 response.
    shop[4].middleware_stack = None
    for _ in range(30):
        assert shop[0].put(path(), json=payload('stale')).status_code == 409
    assert shop[0].put(path(), json=payload(status['revision'])).status_code == 429
    created = shop[0].post('/tokens', headers=interactive_headers(), json={'name': 'Second setup', 'scopes': ['read']})
    assert created.status_code == 200
    token = created.json()
    assert shop[0].put(f"/tokens/{token['id']}/square-setup", headers=interactive_headers(), json={'enabled': True}).status_code == 200
    # A different token from the same client address has its own allowance.
    assert shop[0].put(path(), headers={'X-API-Key': token['token']}, json=payload('stale')).status_code == 409
    # Interactive callers do not spend the delegated token's allowance.
    assert shop[0].put(path(), headers=interactive_headers(), json=payload(status['revision'])).status_code == 200
