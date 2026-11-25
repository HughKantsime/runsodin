"""Square setup administration, refusal audit and write-reply contracts."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import select, text

from core.api_token_policy import SQUARE_SETUP_SCOPE
from core.audit_integrity import verify
from core.models import AuditLog, SystemConfig
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
# Pytest registers review_db (used by shop) and the autouse setup_policy fixture from these imports.
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_square_readonly_workflow import connect, mapped, shop
from tests.test_contracts.test_square_setup_delegation import authenticate, path, payload, setup_policy


def add_token(db, owner, name, scopes):
    from core.db_compat import execute_insert_returning_id
    return execute_insert_returning_id(db, "INSERT INTO api_tokens(user_id,name,token_prefix,token_hash,scopes) "
                                       "VALUES(:uid,:name,:name,'fixture',:scopes)",
                                       {"uid": owner, "name": name, "scopes": json.dumps(scopes)})


def audit_details(db, action):
    return db.execute(select(AuditLog.details).where(AuditLog.action == action).order_by(AuditLog.id)).scalars().all()


def test_signed_in_admin_sees_and_revokes_only_manageable_grants(shop):
    client, _, _, db, *_ = shop
    connect(client)
    authenticate(shop, ['admin'])
    target = add_token(db, 5, 'Other grant', ['read', SQUARE_SETUP_SCOPE])
    db.commit()
    url = f'/tokens/{target}/square-setup'
    listing = '/tokens?managed_square_setup=true'
    def visible(headers):
        return [t for t in client.get(listing, headers=headers).json() if t['id'] == target]
    session = interactive_headers()
    assert not visible(session)
    assert client.put(url, headers=session, json={'enabled': False}).status_code == 403
    db.execute(text('UPDATE users SET group_id=1 WHERE id=5'))
    db.commit()
    assert visible(session) == [{'id': target, 'name': 'Other grant', 'owner_username': 'foreign',
                                 'scopes': [SQUARE_SETUP_SCOPE]}]
    assert not visible({})  # An admin API token still sees only its own tokens.
    assert client.put(url, headers=session, json={'enabled': True}).status_code == 404
    db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    db.execute(text('UPDATE users SET group_id=2 WHERE id=5'))
    db.commit()
    assert visible(session)
    assert client.put(url, headers=session, json={'enabled': False}).json() == {'id': target, 'scopes': []}
    assert json.loads(db.execute(text('SELECT scopes FROM api_tokens WHERE id=:id'), {'id': target}).scalar_one()) == ['read']
    assert not visible(session)
    row, = audit_details(db, 'api_token.square_setup_revoked')
    assert (row['administrator_id'], row['token_owner_user_id'], row['token_id']) == (3, 5, target)


def test_grant_requires_manage_square_and_unexpired_token(shop):
    connect(shop[0])
    token = authenticate(shop, ['read'])
    db = shop[3]
    policy = SystemConfig(key='rbac_permissions', value={'action_access': {'products.square_manage': []}})
    db.add(policy)
    db.commit()
    url = f'/tokens/{token}/square-setup'
    def grant():
        return shop[0].put(url, headers=interactive_headers(), json={'enabled': True})
    refused = grant()
    assert refused.status_code == 403 and 'Manage Square' in refused.json()['detail']
    db.delete(policy)
    db.execute(text("UPDATE api_tokens SET expires_at='2000-01-01' WHERE id=:id"), {'id': token})
    db.commit()
    assert grant().status_code == 409
    assert not audit_details(db, 'api_token.square_setup_granted')


@pytest.mark.parametrize('change', ['demote', 'deactivate', 'null_activity', 'delete', 'erase', 'restore_legacy'])
def test_owner_change_removes_each_grant(shop, change):
    from modules.organizations.routes_users import router
    client, _, _, db, app, *_ = shop
    connect(client)
    authenticate(shop, ['read', SQUARE_SETUP_SCOPE])
    app.include_router(router)
    db.execute(text('UPDATE users SET group_id=1 WHERE id=5'))
    first, second = [add_token(db, 5, name, ['write', SQUARE_SETUP_SCOPE]) for name in ('first', 'second')]
    add_token(db, 5, 'unchanged', ['read'])
    db.commit()
    if change == 'restore_legacy':
        db.execute(text("UPDATE users SET role='operator' WHERE id=5"))
        db.commit()
    headers = interactive_headers()
    if change in ('delete', 'erase'):
        response = client.delete('/users/5/erase' if change == 'erase' else '/users/5', headers=headers)
    else:
        field, value = {'demote': ('role', 'operator'), 'deactivate': ('is_active', False),
                        'null_activity': ('is_active', None), 'restore_legacy': ('role', 'admin')}[change]
        response = client.patch('/users/5', headers=headers, json={field: value})
    assert response.status_code == 200, response.status_code
    if change not in ('delete', 'erase'):
        assert client.patch('/users/5', headers=headers, json={'role': 'admin', 'is_active': True}).status_code == 200
    scopes = [json.loads(value) for value in db.execute(text('SELECT scopes FROM api_tokens WHERE user_id=5 ORDER BY id')).scalars()]
    assert scopes == ([] if change in ('delete', 'erase') else [['write'], ['write'], ['read']])
    rows = audit_details(db, 'api_token.square_setup_revoked')
    assert sorted(row['token_id'] for row in rows) == [first, second]
    if change == 'erase':
        assert all(row == {'token_id': row['token_id'], 'reason': 'owner_erased'} for row in rows)
    else:
        assert all(row['scopes_before'] == ['write', SQUARE_SETUP_SCOPE] and row['scopes_after'] == ['write']
                   and row['token_owner_user_id'] == 5 and row['administrator_id'] == 3 for row in rows)
    assert SQUARE_SETUP_SCOPE in json.loads(db.execute(text('SELECT scopes FROM api_tokens WHERE user_id=3')).scalar_one())


def test_erasure_removes_personal_token_name_from_audit_history(shop):
    client, _, _, db, *_ = shop
    status, _ = connect(client)
    authenticate(shop)
    headers = interactive_headers()
    name = "manager's phone"
    created = client.post('/tokens', headers=headers, json={'name': name, 'scopes': ['read']})
    assert created.status_code == 200
    token = created.json()['id']
    assert client.put(f'/tokens/{token}/square-setup', headers=headers, json={'enabled': True}).status_code == 200
    delegated = {'X-API-Key': created.json()['token']}
    mapping = client.put(path(), headers=delegated, json=payload(status['revision'], approval_note='approved by manager'))
    assert mapping.status_code == 200
    assert client.put(path(True), headers=delegated, json=payload(mapping.json()['revision'], True)).status_code == 200
    assert client.put(f'/tokens/{token}/square-setup', headers=headers, json={'enabled': False}).status_code == 200
    assert client.put(f'/tokens/{token}/square-setup', headers=headers, json={'enabled': True}).status_code == 200
    assert client.delete('/users/2/erase', headers=headers).status_code == 200
    assert audit_details(db, 'api_token_created') == [f"Token '{name}' created"]
    for action in ('api_token.square_setup_granted', 'api_token.square_setup_revoked',
                   'square.product_mapped', 'square.stock_baseline'):
        assert {row['token_name'] for row in audit_details(db, action)} == {name}
    assert audit_details(db, 'square.product_mapped')[0]['approval_note'] == 'approved by [deleted-user]'
    assert client.delete('/users/3/erase', headers=headers).status_code == 200
    assert all(name not in value for row in db.execute(text('SELECT * FROM audit_logs'))
               for value in row if isinstance(value, str))
    assert {'token_id': token, 'reason': 'owner_erased'} in audit_details(db, 'api_token.square_setup_revoked')
    assert verify(db.connection())['ok']


def test_erasure_uses_live_owner_for_legacy_creation_without_actor(review_db):
    from core.audit import log_audit
    from modules.organizations.erasure_audit import erase_token_names

    token = add_token(review_db, 1, 'Old phone', ['read'])
    log_audit(review_db, 'api_token_created', 'api_token', token, "Token 'Old phone' created")
    review_db.commit()
    assert review_db.execute(text("SELECT actor_user_id FROM audit_logs WHERE action='api_token_created'")).one() == (None,)
    erase_token_names(review_db, 1)
    assert audit_details(review_db, 'api_token_created') == ["Token '[deleted]' created"]
    review_db.rollback()
    assert audit_details(review_db, 'api_token_created') == ["Token 'Old phone' created"]
    assert verify(review_db.connection())['ok']


@pytest.mark.parametrize('admin_revokes', [False, True], ids=['owner-revokes', 'admin-revokes'])
def test_erasure_scrubs_revoked_tokens_by_creator(shop, admin_revokes):
    from core.auth import create_access_token

    client, _, _, db, *_ = shop
    connect(client)
    authenticate(shop)
    owner = {'Authorization': 'Bearer ' + create_access_token({'sub': 'student', 'role': 'viewer'})}
    admin = interactive_headers()
    name = "student's ' created and ' revoked phone"
    body = {'name': name, 'scopes': ['read']}
    created = client.post('/tokens', headers=owner, json=body)
    assert created.status_code == 200
    token = created.json()['id']
    assert client.delete(f'/tokens/{token}', headers=admin if admin_revokes else owner).status_code == 200

    other = client.post('/tokens', headers=admin, json=body)
    assert other.status_code == 200
    # SQLite AUTOINCREMENT and PostgreSQL sequences advance even after deleting the highest ID.
    other_id = other.json()['id']
    assert other_id > token
    assert client.delete(f'/tokens/{other_id}', headers=admin).status_code == 200

    lifecycle = select(AuditLog.details).where(AuditLog.entity_type == 'api_token').order_by(AuditLog.id)
    original = [f"Token '{name}' created", f"Token '{name}' revoked"]
    assert db.scalars(lifecycle).all() == original * 2
    assert client.delete('/users/2/erase', headers=admin).status_code == 200  # No token history.
    assert db.scalars(lifecycle).all() == original * 2
    assert client.delete('/users/1/erase', headers=admin).status_code == 200
    assert db.scalars(lifecycle).all() == ["Token '[deleted]' created", "Token '[deleted]' revoked", *original]
    assert verify(db.connection())['ok']


def test_audit_failure_rolls_back_demotion_and_grants(shop, monkeypatch):
    from modules.organizations import routes_users, square_setup_grants
    client, _, _, db, app, *_ = shop
    connect(client)
    authenticate(shop)
    app.include_router(routes_users.router)
    db.execute(text('UPDATE users SET group_id=1 WHERE id=5'))
    for name in ('first', 'second'):
        add_token(db, 5, name, ['read', SQUARE_SETUP_SCOPE])
    db.commit()
    log = square_setup_grants.log_audit
    def fail_second(db, action, entity_type, entity_id, details):
        if details['token_name'] == 'second':
            raise RuntimeError('unavailable audit')
        return log(db, action, entity_type, entity_id, details)
    monkeypatch.setattr(square_setup_grants, 'log_audit', fail_second)
    with pytest.raises(RuntimeError, match='unavailable audit'):
        client.patch('/users/5', headers=interactive_headers(), json={'role': 'viewer'})
    assert db.execute(text('SELECT role FROM users WHERE id=5')).scalar_one() == 'admin'
    assert all(SQUARE_SETUP_SCOPE in json.loads(row) for row in db.execute(text('SELECT scopes FROM api_tokens WHERE user_id=5')).scalars())
    assert not audit_details(db, 'api_token.square_setup_revoked')


def test_refusal_window_serializes_concurrent_requests(shop, monkeypatch):
    from modules.orders import square_setup_refusals as refusals
    client, _, _, db, *_ = shop
    status = mapped(shop)
    token = authenticate(shop)
    body = payload(status['revision'], variation_id='V2')
    gate = Barrier(2)
    def request():
        gate.wait(timeout=10)
        return client.put(path(), json=body).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(request) for _ in range(2)]
        assert [r.result(timeout=20) for r in results] == [409, 409]
    row = {'token_id': token, 'product_id': 101, 'reason': 'already_mapped'}
    assert audit_details(db, 'square.setup_refused') == [row]
    clock = refusals.datetime
    class NextMinute(clock):
        @classmethod
        def now(cls, tz=None):
            return clock.now(tz) + timedelta(seconds=61)
    monkeypatch.setattr(refusals, 'datetime', NextMinute)
    assert client.put(path(), json=body).status_code == 409
    assert audit_details(db, 'square.setup_refused') == [row, row]


def test_refusal_audits_separate_reasons_tokens_and_products(shop):
    client, _, _, db, *_ = shop
    status = mapped(shop)
    token = authenticate(shop, ['read'])
    assert client.put(path(), json=payload(status['revision'])).status_code == 403
    assert client.put(f'/tokens/{token}/square-setup', headers=interactive_headers(), json={'enabled': True}).status_code == 200
    body = payload(status['revision'], approval_note='password=synthetic')
    assert client.put(path(), json=body).status_code == 422
    assert client.put(path().replace('101', '104'), json=body).status_code == 422
    assert client.put(path(True), json={'quantity': 'invalid'}).status_code == 422
    second = client.post('/tokens', headers=interactive_headers(), json={'name': 'Second', 'scopes': ['read']}).json()
    assert client.put(path(), headers={'X-API-Key': second['token']}, json=body).status_code == 403
    assert audit_details(db, 'square.setup_refused') == [
        {'token_id': token, 'product_id': 101, 'reason': 'scope_required'},
        {'token_id': token, 'product_id': 101, 'reason': 'approval_note_sensitive'},
        {'token_id': token, 'product_id': 104, 'reason': 'approval_note_sensitive'},
        {'token_id': token, 'product_id': 101, 'reason': 'invalid_request'},
        {'token_id': second['id'], 'product_id': 101, 'reason': 'scope_required'}]

    actors = db.execute(text("SELECT DISTINCT actor_auth_kind, actor_user_id FROM audit_logs "
                             "WHERE action='square.setup_refused'")).all()
    assert actors == [('user_api_token', 3)]


@pytest.mark.parametrize('read_scope', [None, 'read:printers', 'read', 'admin'])
def test_reply_only_exposes_the_written_item_without_square_read_access(shop, read_scope):
    from modules.orders import square_store as store
    client, _, _, db, *_ = shop
    connect(client)
    doc, encoded = store.load(db, 1)
    doc['mappings']['104'] = {'location_id': 'L1', 'variation_id': 'V2', 'item_name': 'Other', 'variation_name': 'Other'}
    status = store.save(db, 1, doc, encoded)
    db.commit()
    authenticate(shop, [SQUARE_SETUP_SCOPE] + ([read_scope] if read_scope else []))
    result = client.put(path(), json=payload(status['revision']))
    assert result.status_code == 200
    mapping = result.json()
    result = client.put(path(True), json=payload(mapping['revision'], True))
    assert result.status_code == 200
    baseline = result.json()
    assert not audit_details(db, 'square.setup_refused')
    if read_scope in ('read', 'admin'):
        assert baseline['pending_additions'] == []
        for connection in (mapping, baseline['connection']):
            assert set(connection) == {'connected', 'environment', 'merchant_id', 'mappings', 'revision',
                                       'stock_writes_enabled', 'square_writes_enabled'}
            assert {m['product_id'] for m in connection['mappings']} == {101, 104}
        return
    item = {'product_id': 101, 'location_id': 'L1', 'variation_id': 'V1', 'tracked': True}
    assert mapping == {'revision': mapping['revision'], 'mappings': [item]}
    connection = baseline['connection']
    stored = store.load(db, 1)[0]['mappings']['101']['stock_baseline']
    fields = {key: stored[key] for key in ('version', 'at', 'quantity')}
    assert baseline == {'connection': {'revision': connection['revision'], 'mappings': [{**item, 'stock_baseline': fields}]}}
    repeated = client.put(path(), json=payload(connection['revision']))
    assert repeated.json() == {'revision': connection['revision'], 'mappings': [{**item, 'tracked': None}]}

def test_admin_token_demotion_withdraws_scope_through_runtime_guard(shop):
    from core.credential_write_guard import install_credential_write_guard
    from modules.organizations.routes_users import router
    client, _, _, db, app, *_ = shop
    connect(client)
    authenticate(shop, ['admin'])
    app.include_router(router)
    db.execute(text('UPDATE users SET group_id=1 WHERE id=5'))
    token_id = add_token(db, 5, 'target', ['read', SQUARE_SETUP_SCOPE])
    db.commit()
    install_credential_write_guard()
    assert client.patch('/users/5', json={'role': 'viewer'}).status_code == 200
    assert json.loads(db.execute(text('SELECT scopes FROM api_tokens WHERE id=:id'), {'id': token_id}).scalar_one()) == ['read']
    row, = audit_details(db, 'api_token.square_setup_revoked')
    assert row['auth_kind'] == 'user_api_token' and row['token_id'] == token_id


def test_runtime_guard_only_admits_exact_square_scope_subtraction():
    from core.api_token_policy import REMOVE_INELIGIBLE_SETUP_SQL, SET_TOKEN_SCOPES_SQL
    from core.credential_write_guard import guarded_write_target
    from sqlalchemy.dialects import postgresql, sqlite
    before = json.dumps(['read', SQUARE_SETUP_SCOPE])
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        sql = str(text(REMOVE_INELIGIBLE_SETUP_SQL).compile(dialect=dialect))
        without_account_check = str(text(SET_TOKEN_SCOPES_SQL).compile(dialect=dialect))
        for updated in (['read'], ['admin'], ['read', 'write'], ['read', SQUARE_SETUP_SCOPE], []):
            after = json.dumps(updated)
            params = (after, 1, 3, before) if dialect.name == 'sqlite' else {
                'scopes': after, 'id': 1, 'uid': 3, 'previous': before}
            assert guarded_write_target(without_account_check, params) == 'api_tokens'
            assert guarded_write_target(sql, params) == (None if updated == ['read'] else 'api_tokens')


@pytest.mark.parametrize('dialect_name', ['sqlite', 'postgresql'])
def test_runtime_guard_rejects_malformed_subtraction(dialect_name):
    from core.api_token_policy import REMOVE_INELIGIBLE_SETUP_SQL
    from core.credential_write_guard import guarded_write_target
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = sqlite.dialect() if dialect_name == 'sqlite' else postgresql.dialect()
    sql = str(text(REMOVE_INELIGIBLE_SETUP_SQL).compile(dialect=dialect))

    def bindings(before, after):
        named = {'scopes': json.dumps(after), 'id': 1, 'uid': 3, 'previous': json.dumps(before)}
        return tuple(named.values()) if dialect_name == 'sqlite' else named

    for before, after in [
        (['read:cameras', SQUARE_SETUP_SCOPE], ['read:cameras']),
        (['read'], ['read']),
        (['read', SQUARE_SETUP_SCOPE], ['read', 'write']),
        (['read', 'write', SQUARE_SETUP_SCOPE], ['read']),
    ]:
        assert guarded_write_target(sql, bindings(before, after)) == 'api_tokens'

    valid = bindings(['read', SQUARE_SETUP_SCOPE], ['read'])
    assert guarded_write_target(sql + ' ', valid) == 'api_tokens'
    if dialect_name == 'sqlite':
        wrong_order = (valid[1], valid[0], valid[2], valid[3])
        malformed = (wrong_order, (valid[0], None, valid[2], valid[3]), valid[:-1], (*valid, 4))
    else:
        malformed = ({**valid, 'id': valid['scopes'], 'scopes': valid['id']},
                     {**valid, 'uid': None},
                     {key: value for key, value in valid.items() if key != 'id'},
                     {**valid, 'extra': 4})
    for params in malformed:
        assert guarded_write_target(sql, params) == 'api_tokens'


def test_family_license_gate_is_audited_on_the_mounted_route(shop, tier_license):
    from modules.orders.routes import router
    client, _, calls, db, app, *_ = shop
    status = mapped(shop)
    token = authenticate(shop)
    app.include_router(router, prefix='/api')
    tier_license(None)
    calls.clear()
    for baseline in (True, False):
        assert client.put('/api' + path(baseline), json=payload(status['revision'], baseline)).status_code == 403
    assert not calls
    assert audit_details(db, 'square.setup_refused') == [
        {'token_id': token, 'product_id': 101, 'reason': 'license_required'}]

def test_demotion_while_grant_waits_requires_a_new_grant(shop, monkeypatch):
    from modules.organizations import routes_sessions, routes_users
    client, _, _, db, app, *_ = shop
    connect(client)
    token = authenticate(shop, ['read'])
    app.include_router(routes_users.router)
    started, resume = Event(), Event()
    lock = routes_sessions.lock_accounts
    def delayed_lock(*args):
        started.set()
        assert resume.wait(timeout=10)
        return lock(*args)
    monkeypatch.setattr(routes_sessions, 'lock_accounts', delayed_lock)
    headers = interactive_headers()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.put, f'/tokens/{token}/square-setup', headers=headers, json={'enabled': True})
        try:
            assert started.wait(timeout=10)
            assert client.patch('/users/3', headers=headers, json={'role': 'operator'}).status_code == 200
        finally:
            resume.set()
        assert request.result(timeout=10).status_code == 403
    assert json.loads(db.execute(text('SELECT scopes FROM api_tokens WHERE id=:id'), {'id': token}).scalar_one()) == ['read']
    assert not audit_details(db, 'api_token.square_setup_granted')
