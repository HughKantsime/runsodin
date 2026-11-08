"""B1 acceptance: real signed artifacts, durable policy and actual auth carriers.

Hardware/stream authority (B2/B3) is deliberately outside this suite.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import time

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import text, event
from sqlalchemy.orm import Session
from starlette.requests import Request

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_clock import clock_env, BASE
from tests.test_contracts.test_license_user_limits import signed_license
from core import license_lock as policy

PASSWORD = 'LicenseTestPassword123!'


@pytest.fixture
def policy_db(review_db, tier_license, monkeypatch):
    from core.auth import hash_password
    from core import db as db_module
    # Establish two eligible recovery principals before enabling the guard.
    hashed = hash_password(PASSWORD)
    review_db.execute(text('UPDATE users SET password_hash=:h'), {'h': hashed})
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id IN (3,5)'))
    review_db.commit()
    tier_license('enterprise', max_users=1, max_printers=1)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    monkeypatch.setattr(db_module, 'SessionLocal', lambda: Session(review_db.get_bind()))
    monkeypatch.setattr(db_module, 'current_engine', lambda: review_db.get_bind())
    monkeypatch.setattr(db_module, 'engine', review_db.get_bind())
    policy._cache.clear()
    policy.reconcile(review_db)
    review_db.commit()
    yield review_db
    review_db.rollback()
    policy._cache.clear()


def status(db):
    p = policy.selection_status(db)
    db.commit()
    return p


def pick(db, users, printers=None, recovery=None):
    p = status(db)
    result = policy.replace_selection(db, printers=printers if printers is not None else p['printers'],
        users=users, expected_revision=p['revision'], actor=3, recovery_user_id=recovery)
    db.commit()
    return result


def test_defaults_idempotence_and_real_caps(policy_db):
    first = status(policy_db)
    assert first['users'] == [3] and first['printers'] == [9]
    assert first['entitlement']['state'] == 'verified_current'
    assert first['usage'] == {'users': 5, 'printers': 2}
    assert status(policy_db)['revision'] == first['revision']
    assert policy.snapshot(policy_db).enforcing
    with pytest.raises(policy.LicenseLockError) as exc:
        policy.require_user(policy.snapshot(policy_db), 1)
    assert exc.value.detail['code'] == 'license_user_locked'


@pytest.mark.parametrize('bad', [[3, 3], [True], [0], [999], ['3'], [3, 5]])
def test_invalid_picks_are_not_silently_repaired(policy_db, bad):
    old = status(policy_db)
    with pytest.raises(policy.LicenseLockError) as exc:
        policy.replace_selection(policy_db, users=bad, printers=[9], expected_revision=old['revision'], actor=3)
    assert exc.value.status_code == 422
    policy_db.rollback()
    assert status(policy_db)['users'] == [3]


def test_concurrent_cas_both_lists_atomic(policy_db):
    original = status(policy_db)
    engine = policy_db.get_bind()
    def save(printer):
        with Session(engine) as db:
            try:
                p = policy.replace_selection(db, users=[3], printers=[printer], expected_revision=original['revision'], actor=3)
                db.commit()
                return 200, p
            except policy.LicenseLockError as exc:
                db.rollback()
                return exc.status_code, exc.detail
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [9, 10]))
    assert sorted(r[0] for r in results) == [200, 409]
    winner = next(p for code, p in results if code == 200)
    assert status(policy_db)['printers'] == winner['printers']
    assert status(policy_db)['users'] == [3]


def test_vacancy_and_transactional_generation(policy_db):
    old = status(policy_db)
    policy_db.execute(text('UPDATE printers SET is_active=FALSE WHERE id=9'))
    policy_db.commit()
    new = status(policy_db)
    assert new['printers'] == [10]
    assert new['membership_generation'] > old['membership_generation']
    assert new['revision'] > old['revision']
    policy_db.execute(text('UPDATE printers SET is_active=TRUE WHERE id=9'))
    policy_db.rollback()
    assert status(policy_db)['membership_generation'] == new['membership_generation']


@pytest.mark.parametrize('mutation', ["role='viewer'", 'is_active=FALSE', 'group_id=1', "password_hash='sso-placeholder'"])
def test_last_local_recovery_cannot_be_removed(policy_db, mutation):
    policy_db.execute(text("UPDATE users SET role='viewer' WHERE id=5"))
    policy_db.commit()
    policy_db.execute(text('UPDATE users SET ' + mutation + ' WHERE id=3'))
    with pytest.raises(policy.LicenseLockError) as exc:
        policy_db.commit()
    assert exc.value.detail['reason'] == 'last_local_recovery_principal'
    policy_db.rollback()
    assert policy.eligible_local(policy_db.execute(text('SELECT * FROM users WHERE id=3')).mappings().one())


def test_handoff_reserves_one_seat(policy_db):
    p = pick(policy_db, [5], recovery=5)
    assert p['users'] == [5] and p['recovery_user_id'] == 5
    assert not policy.user_allowed(policy.snapshot(policy_db), 3)
    policy_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"))
    policy_db.commit()
    assert status(policy_db)['users'] == [5]


@pytest.mark.parametrize('cap', [0, None, 3])
def test_zero_null_and_explicit_caps(policy_db, tier_license, cap):
    tier_license('enterprise', max_users=cap, max_printers=cap)
    p = status(policy_db)
    expected = 0 if cap == 0 else 5 if cap is None else 3
    assert len(p['users']) == expected
    if cap == 0:
        assert not policy.user_allowed(policy.snapshot(policy_db), 3)
        assert not p['restricted_recovery_enabled']
    else:
        assert p['recovery_user_id'] in p['users']


def test_paid_lapse_and_rollout_are_independent_off_switches(policy_db, tier_license, clock_env, monkeypatch):
    import license_manager as lm
    wall, tick, _, new, _ = clock_env
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('pro', expires_at=(BASE + timedelta(seconds=10)).isoformat())
    status(policy_db)
    tick[0] += 10; wall[0] += timedelta(seconds=10)
    assert lm.get_license().verification_state == 'verified_expired'
    assert not policy.snapshot(policy_db).enforcing
    monkeypatch.setenv('ODIN_LICENSE_PAID_LAPSE_ENABLED', 'true')
    assert policy.snapshot(policy_db).enforcing
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    assert not policy.snapshot(policy_db).enforcing


def test_trial_lapse_requires_reliable_time(policy_db, tier_license, clock_env, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new(); monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('pro', trial=True, expires_at=(BASE + timedelta(seconds=10)).isoformat())
    tick[0] += 10; wall[0] += timedelta(seconds=10)
    p = status(policy_db)
    assert p['entitlement']['state'] == 'verified_expired'
    assert p['entitlement']['max_printers'] == 5
    assert p['users'] == [3]


def test_uncertainty_lease_survives_restart_and_never_renews(policy_db, tier_license, clock_env, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new(); monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('enterprise', max_users=1, max_printers=1)
    before = status(policy_db)
    tier_license(None)
    first = status(policy_db)
    evidence = policy._read(policy_db, policy.EVIDENCE_KEY)
    deadline = evidence['uncertainty_deadline']
    assert first['users'] == before['users'] and first['entitlement']['enforce']
    for _ in range(3):
        tick[0] += 3600; wall[0] += timedelta(hours=1)
        clock.sample(persist=True)
        clock = new(); monkeypatch.setattr(lm, 'license_clock', lambda: clock)
        lm._cached_license = None; policy._cache.clear()
        assert status(policy_db)['entitlement']['enforce']
        assert policy._read(policy_db, policy.EVIDENCE_KEY)['uncertainty_deadline'] == deadline
    tick[0] += 86400; wall[0] += timedelta(days=1)
    p = status(policy_db)
    assert not p['entitlement']['enforce'] and p['warning']
    assert p['users'] == before['users']  # saved picks retained after lease
    tier_license('enterprise', max_users=None)
    assert not status(policy_db)['warning']
    assert policy.user_allowed(policy.snapshot(policy_db), 1)


def test_cold_boot_gap_ends_lease(policy_db, tier_license, clock_env, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new(); monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('enterprise', max_users=1)
    status(policy_db); tier_license(None); status(policy_db)
    wall[0] += timedelta(days=3)
    clock = new(); monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    assert not status(policy_db)['entitlement']['enforce']


@pytest.mark.parametrize('field,value', [('max_users', True), ('max_printers', -1), ('max_users', 1.5),
    ('features', {}), ('tier', 'unknown'), ('schema_version', 8)])
def test_malformed_but_signed_expired_payload_is_not_lapse(signed_license, field, value):
    _, info = signed_license(**{'expires_at': '2000-01-01', field: value})
    assert info.verification_state == 'verification_unavailable'
    assert not info.valid and info.verified_digest is None


def test_forward_jump_does_not_poison_anchor_before_recovery(clock_env):
    wall, tick, store, new, _ = clock_env
    clock = new(); clock.sample(persist=True)
    wall[0] += timedelta(days=365)
    for _ in range(3):
        tick[0] += 1
        assert clock.sample(persist=True)[1]
    assert store.read() == BASE
    # A restart has no evidence that this is a running-process jump.
    clock = new()
    assert not clock.sample(persist=True)[1]


def test_nonfinite_clock_is_quarantined(clock_env):
    wall, tick, store, new, _ = clock_env
    clock = new(); clock.sample(persist=True)
    tick[0] = float('nan')
    assert clock.sample(persist=True)[1]
    assert store.read() == BASE


@pytest.fixture
def auth_client(policy_db, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations import routes_auth, routes_mfa_enrollment, routes_oidc
    from modules.push.routes import biometric
    from modules.system import routes_health
    routes_auth.limiter._storage.reset()
    app = FastAPI()
    for router in (routes_auth.router, routes_mfa_enrollment.router, routes_oidc.router, biometric.router, routes_health.router):
        app.include_router(router, prefix='/api/v1')
        app.include_router(router, prefix='/api')
    @app.get('/api/v1/private')
    def private(user=Depends(get_current_user)):
        return {'id': user['id'] if user else None}
    def db():
        with Session(policy_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = db
    return TestClient(app)


def token(username='student', **claims):
    from core.auth import create_access_token
    return create_access_token({'sub': username, 'role': 'admin' if username == 'admin' else 'viewer', **claims})


@pytest.mark.parametrize('carrier', ['bearer', 'cookie', 'global', 'scoped', 'query', 'native'])
def test_existing_credential_carriers_lock_actual_owner(policy_db, auth_client, monkeypatch, carrier):
    from core import dependencies as deps
    from core.auth import hash_password
    credential = token()
    if carrier == 'query':
        call = lambda: deps.validate_access_token(credential, policy_db)
    elif carrier == 'native':
        call = lambda: deps.validate_perimeter_access_token(credential, policy_db)
    elif carrier == 'global':
        # Legacy key still maps to oldest active admin 3; select 5 instead.
        pick(policy_db, [5], recovery=5)
        monkeypatch.setenv('API_KEY', 'synthetic-global-key')
        call = lambda: deps.resolve_legacy_api_key('synthetic-global-key', policy_db)
    elif carrier == 'scoped':
        credential = 'odin_abcdefghijk-synthetic'
        policy_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(1,'test',:h,:p,'[\"read\"]')"),
                          {'h': hash_password(credential), 'p': credential[:10]})
        policy_db.commit()
        call = lambda: deps.resolve_user_api_token(credential, policy_db)
    else:
        if carrier == 'cookie':
            auth_client.cookies.set('session', credential)
            # A locked valid cookie must not fall through to selected bearer.
            headers = {'Authorization': 'Bearer ' + token('admin')}
        else:
            headers = {'Authorization': 'Bearer ' + credential}
        response = auth_client.get('/api/v1/private', headers=headers)
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'license_user_locked'
        return
    with pytest.raises(policy.LicenseLockError) as exc:
        call()
    assert exc.value.detail['code'] == 'license_user_locked'


def test_password_login_and_locked_logout(auth_client, policy_db):
    denied = auth_client.post('/api/v1/auth/login', data={'username': 'student', 'password': PASSWORD})
    assert denied.status_code == 403 and 'set-cookie' not in denied.headers
    allowed = auth_client.post('/api/v1/auth/login', data={'username': 'admin', 'password': PASSWORD})
    assert allowed.status_code == 200, allowed.text
    pick(policy_db, [5], recovery=5)
    assert auth_client.get('/api/v1/private').status_code == 403
    assert auth_client.post('/api/v1/auth/logout').status_code == 200
    assert policy_db.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar() == 0


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
def test_selection_api_interactive_superadmin_and_cas(auth_client, policy_db, prefix):
    headers = {'Authorization': 'Bearer ' + token('admin')}
    response = auth_client.get(prefix + '/license/selection', headers=headers)
    assert response.status_code == 200, response.text
    p = response.json()
    saved = auth_client.put(prefix + '/license/selection', headers=headers, json={
        'printers': [10], 'users': [3], 'expected_revision': p['revision']})
    assert saved.status_code == 200, saved.text
    stale = auth_client.put(prefix + '/license/selection', headers=headers, json={
        'printers': [9], 'users': [3], 'expected_revision': p['revision']})
    assert stale.status_code == 409
    assert 'signed_envelope' not in response.text
    public = auth_client.get(prefix + '/license').json()
    assert not {'users', 'printers', 'recovery_user_id', 'signed_envelope'} & public.keys()


def test_snapshot_query_budget(policy_db):
    policy.snapshot(policy_db)
    queries = []
    engine = policy_db.get_bind()
    def count(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        for _ in range(10):
            snap = policy.snapshot(policy_db)
            for uid in range(1000):
                policy.user_allowed(snap, uid)
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(queries) == 10
    assert all('system_config' in sql and 'SELECT' in sql for sql in queries)


def test_mfa_challenge_swap_refuses_full_session(auth_client, policy_db, monkeypatch):
    import pyotp
    from cryptography.fernet import Fernet
    from core.crypto import encrypt
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    secret = pyotp.random_base32()
    policy_db.execute(text('UPDATE users SET mfa_enabled=TRUE,mfa_secret=:secret WHERE id=3'), {'secret': encrypt(secret)})
    policy_db.commit()
    pending = auth_client.post('/api/v1/auth/login', data={'username': 'admin', 'password': PASSWORD})
    assert pending.status_code == 200 and pending.json()['mfa_required']
    pick(policy_db, [5], recovery=5)
    response = auth_client.post('/api/v1/auth/mfa/verify', json={'mfa_token': pending.json()['access_token'], 'code': pyotp.TOTP(secret).now()})
    assert response.status_code == 403, response.text
    assert 'set-cookie' not in response.headers
    assert policy_db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 0


def test_enrollment_swap_confirm_refuses_but_cancel_remains(auth_client, policy_db, monkeypatch):
    import pyotp
    from cryptography.fernet import Fernet
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    policy_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')")); policy_db.commit()
    pending = auth_client.post('/api/v1/auth/login', data={'username': 'admin', 'password': PASSWORD})
    assert pending.status_code == 200 and pending.json()['mfa_setup_required']
    headers = {'Authorization': 'Bearer ' + pending.json()['access_token']}
    setup = auth_client.post('/api/v1/auth/mfa/enrollment/setup', headers=headers)
    assert setup.status_code == 200, setup.text
    pick(policy_db, [5], recovery=5)
    response = auth_client.post('/api/v1/auth/mfa/enrollment/confirm', headers=headers, json={'code': pyotp.TOTP(setup.json()['secret']).now()})
    assert response.status_code == 403, response.text
    assert policy_db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 0
    policy_db.rollback()
    assert auth_client.post('/api/v1/auth/mfa/enrollment/cancel', headers=headers).status_code == 200


def test_biometric_mint_refresh_and_revoke(auth_client, policy_db):
    from modules.push.models import BiometricToken
    BiometricToken.__table__.create(policy_db.get_bind(), checkfirst=True)
    headers = {'Authorization': 'Bearer ' + token('admin')}
    mint = auth_client.post('/api/v1/auth/biometric-token?device_id=test-device', headers=headers)
    assert mint.status_code == 200, mint.text
    raw = mint.json()['refresh_token']
    pick(policy_db, [5], recovery=5)
    assert auth_client.post('/api/v1/auth/biometric-token?device_id=other', headers=headers).status_code == 403
    refresh = auth_client.post('/api/v1/auth/biometric-refresh', json={'refresh_token': raw, 'device_id': 'test-device'})
    assert refresh.status_code == 403, refresh.text
    assert 'access_token' not in refresh.json()
    assert auth_client.delete('/api/v1/auth/biometric-token?device_id=test-device', headers=headers).status_code == 204


def test_oidc_final_code_exchange_rechecks_and_consumes(auth_client, policy_db, monkeypatch):
    from cryptography.fernet import Fernet
    from core.crypto import encrypt
    from modules.organizations.routes_oidc import oidc_code_key, OIDC_BINDING_COOKIE
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    binding, code = 'a' * 64, 'test-code'
    policy_db.execute(text('INSERT INTO oidc_auth_codes(code,access_token,expires_at) VALUES(:c,:t,:e)'),
        {'c': oidc_code_key(code, binding), 't': encrypt(token('admin')), 'e': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()})
    policy_db.commit()
    pick(policy_db, [5], recovery=5)
    auth_client.cookies.set(OIDC_BINDING_COOKIE, binding)
    response = auth_client.post('/api/v1/auth/oidc/exchange', json={'code': code})
    assert response.status_code == 403, response.text
    assert 'set-cookie' not in response.headers
    assert auth_client.post('/api/v1/auth/oidc/exchange', json={'code': code}).status_code == 400


def test_ws_batch_revokes_only_locked_recipient(policy_db):
    from core.app_websocket import ConnectionManager
    class Socket:
        def __init__(self): self.closed = None; self.sent = []
        async def accept(self): pass
        async def close(self, code, reason): self.closed = code
        async def send_json(self, data): self.sent.append(data)
    manager = ConnectionManager()
    selected, locked = Socket(), Socket()
    async def run():
        await manager.connect(selected, {'id': 3, 'role': 'admin', 'group_id': None})
        await manager.connect(locked, {'id': 1, 'role': 'viewer', 'group_id': 1})
        return await manager.broadcast({'type': 'license.policy_changed', 'data': {}})
    assert asyncio.run(run()) == 1
    assert locked.closed == 4403 and selected.closed is None
    assert manager.principal_allowed({'id': 3})
    pick(policy_db, [5], recovery=5)
    assert not manager.principal_allowed({'id': 3})


def test_org_admin_and_scoped_token_cannot_pick(auth_client, policy_db, tier_license):
    from core.auth import hash_password
    tier_license('enterprise', max_users=None)
    # Active org admin stays in its org; selection is install-wide.
    policy_db.execute(text("UPDATE users SET role='admin',group_id=1 WHERE id=1"))
    credential = 'odin_policy-test-only'
    policy_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'test',:h,:p,'[\"admin\"]')"),
                      {'h': hash_password(credential), 'p': credential[:10]})
    policy_db.commit()
    assert auth_client.get('/api/v1/license/selection', headers={'Authorization': 'Bearer ' + token()}).status_code == 403
    assert auth_client.get('/api/v1/license/selection', headers={'X-API-Key': credential}).status_code == 403


def test_rollout_off_preserves_all_auth(policy_db, auth_client, monkeypatch):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    assert auth_client.get('/api/v1/private', headers={'Authorization': 'Bearer ' + token()}).status_code == 200
    assert auth_client.post('/api/v1/auth/login', data={'username': 'student', 'password': PASSWORD}).status_code == 200


@pytest.mark.parametrize('url', ['/ws', '/api/v1/ws'])
def test_real_ws_admission_and_idle_revocation(policy_db, monkeypatch, url):
    from core.app import create_app
    from core.auth import create_access_token
    from starlette.websockets import WebSocketDisconnect
    credential = create_access_token({'sub': 'admin', 'role': 'admin', 'ws': True,
        'user_id': 3, 'capability_snapshot_id': 'owned-test'}, timedelta(minutes=5))
    client = TestClient(create_app())  # no lifespan/hardware services
    with client.websocket_connect(url + '?token=' + credential) as ws:
        ws.send_text('ping')
        assert ws.receive_text() == 'pong'
        pick(policy_db, [5], recovery=5)
        start = time.monotonic()
        # No events or client messages: the server must notice the changed seat.
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                ws.receive_json()
        assert closed.value.code == 4403
        assert time.monotonic() - start < 5
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(url + '?token=' + credential) as ws:
            ws.receive_text()
    assert closed.value.code == 4403


def test_perimeter_blocks_cached_success_before_replay(policy_db, monkeypatch):
    from core.app import _register_http_middleware
    from core.middleware import idempotency
    from fastapi.responses import JSONResponse
    called = []
    async def cached(request, call_next):
        called.append(True)
        return JSONResponse({'cached': True})
    monkeypatch.setattr(idempotency, 'idempotency_middleware', cached)
    app = FastAPI()
    _register_http_middleware(app)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + token('admin'), 'Idempotency-Key': 'test-cache'}
    assert client.post('/api/v1/test', headers=headers).status_code == 200
    pick(policy_db, [5], recovery=5)
    denied = client.post('/api/v1/test', headers=headers)
    assert denied.status_code == 403 and len(called) == 1
    assert denied.headers['X-ODIN-License-Lock'] == 'license_user_locked'


def test_no_eligible_local_superadmin_grants_no_full_recovery_seat(policy_db, monkeypatch):
    # Simulate a restored legacy installation before enabling rollout.
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    policy_db.execute(text("UPDATE users SET password_hash='sso-only' WHERE role='admin'")); policy_db.commit()
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    p = status(policy_db)
    assert p['users'] == []
    assert p['recovery_user_id'] is None
    assert p['warning'] == 'local_recovery_bootstrap_required'


def test_corrupt_policy_does_not_become_expiry_or_leak_private_fields(policy_db):
    old = status(policy_db)
    policy._write(policy_db, policy.POLICY_KEY, {'schema_version': 99, 'revision': old['revision'], 'signed_envelope': 'must-not-return'})
    policy_db.commit()
    p = status(policy_db)
    assert p['revision'] > old['revision']
    assert p['entitlement']['state'] == 'verified_current'
    assert 'signed_envelope' not in p
    assert p['warning'] == 'license_policy_reconstructed'


def test_thousand_member_snapshot_benchmark(policy_db):
    from statistics import quantiles
    # One batch each; the existing fixture has five users and two printers.
    policy_db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(:id,:name,'placeholder','viewer',TRUE)"),
                      [{'id': i, 'name': f'bench-user-{i}'} for i in range(100, 1095)])
    policy_db.execute(text("INSERT INTO printers(id,name,is_active) VALUES(:id,:name,TRUE)"),
                      [{'id': i, 'name': f'bench-printer-{i}'} for i in range(100, 1098)])
    policy_db.commit()
    samples, rebuilds, queries = [], [], []
    def count(conn, cursor, statement, parameters, context, executemany): queries.append(statement)
    event.listen(policy_db.get_bind(), 'before_cursor_execute', count)
    try:
        for _ in range(100):
            start = time.perf_counter(); status(policy_db); rebuilds.append((time.perf_counter() - start) * 1000)
        policy.snapshot(policy_db)
        queries.clear()
        for _ in range(100):
            start = time.perf_counter(); policy.snapshot(policy_db); samples.append((time.perf_counter() - start) * 1000)
    finally:
        event.remove(policy_db.get_bind(), 'before_cursor_execute', count)
    warm95, rebuild95 = quantiles(samples, n=20)[-1], quantiles(rebuilds, n=20)[-1]
    print(f'SQLite 1000 printers/1000 users: warm p95={warm95:.2f}ms; rebuild p95={rebuild95:.2f}ms; warm queries={len(queries)}/100')
    assert warm95 <= 5 and rebuild95 <= 50, (samples, rebuilds)
    assert len(queries) == 100


def test_error_reload_backoff_and_preserved_mtime(policy_db, tier_license, monkeypatch):
    import os
    from pathlib import Path
    import license_manager as lm
    ticks = [100.0]
    # Replace only the module's elapsed clock, not the process clock used elsewhere.
    from types import SimpleNamespace
    monkeypatch.setattr(lm, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))
    tier_license('enterprise', max_users=1)
    path = Path(lm.LICENSE_DIR) / lm.LICENSE_FILENAME
    original = path.read_bytes(); stamp = path.stat().st_mtime_ns
    path.write_bytes(b'invalid'); os.utime(path, ns=(stamp, stamp))
    ticks[0] += 1
    assert lm.get_license().verification_state == 'verification_unavailable'
    attempts = []
    original_load = lm.load_license
    def load(): attempts.append(ticks[0]); return original_load()
    monkeypatch.setattr(lm, 'load_license', load)
    for advance in (1, 4, 1, 14, 1, 29, 1):
        ticks[0] += advance
        lm.get_license()
    assert len(attempts) == 4 and [round(b-a) for a,b in zip(attempts, attempts[1:])] == [5, 15, 30]
    path.write_bytes(original); os.utime(path, ns=(stamp, stamp))
    ticks[0] += 1
    assert lm.get_license().valid


@pytest.mark.parametrize('fault', ['discovery', 'stat', 'identity', 'read'])
def test_read_stat_discovery_identity_faults_are_unavailable(policy_db, monkeypatch, fault):
    import builtins
    import license_manager as lm
    lm._cached_license = None
    def broken(*args, **kwargs): raise PermissionError('owned synthetic failure')
    if fault == 'discovery': monkeypatch.setattr(lm, '_find_license_file', broken)
    elif fault == 'stat': monkeypatch.setattr(lm.os, 'stat', broken)
    elif fault == 'identity': monkeypatch.setattr(lm, '_read_existing_installation_id', broken)
    else:
        original = builtins.open
        def fail_license(path, *args, **kwargs):
            if str(path).endswith('odin.license'): return broken()
            return original(path, *args, **kwargs)
        monkeypatch.setattr(builtins, 'open', fail_license)
    info = lm.get_license()
    assert not info.valid and info.verification_state == 'verification_unavailable'


@pytest.mark.parametrize('carrier', ['bearer_jwt', 'session_jwt', 'legacy_global_api_key', 'user_api_token'])
def test_existing_mjpeg_revalidation_uses_current_user_selection(policy_db, monkeypatch, carrier):
    from modules.printers.routes_camera_snapshot import _mjpeg_check
    from core.auth import hash_password
    if carrier == 'legacy_global_api_key':
        credential = 'synthetic-key'; monkeypatch.setenv('API_KEY', credential)
    elif carrier == 'user_api_token':
        credential = 'odin_mjpeg-test-only'
        policy_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'test',:h,:p,'[\"read:printers\"]')"),
                          {'h': hash_password(credential), 'p': credential[:10]}); policy_db.commit()
    else:
        credential = token('admin')
    pick(policy_db, [5], recovery=5)
    with pytest.raises(policy.LicenseLockError):
        _mjpeg_check(policy_db.get_bind(), carrier, credential, 3, 9)


@pytest.mark.parametrize('change', [{'role': 'viewer'}, {'is_active': False}, {'group_id': 1}])
def test_actual_user_update_route_preserves_last_recovery(policy_db, change):
    from modules.organizations.routes_users import update_user, UserUpdateRequest
    policy_db.execute(text("UPDATE users SET password_hash='sso-only' WHERE id=5")); policy_db.commit()
    actor = {'id': 3, 'role': 'admin', 'group_id': None, '_auth_kind': 'bearer_jwt'}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        asyncio.run(update_user(3, UserUpdateRequest(**change), actor, None, policy_db))
    # Existing Education history protection may refuse tenant changes first.
    assert refused.value.status_code in {409, 422}
    policy_db.rollback()
    assert policy.eligible_local(policy_db.execute(text('SELECT * FROM users WHERE id=3')).mappings().one())


def test_password_reset_locked_user_is_repair_only(auth_client, policy_db):
    from modules.push.models import BiometricToken
    BiometricToken.__table__.create(policy_db.get_bind(), checkfirst=True)
    from modules.organizations.password_reset import reset_token_digest
    reset = 'owned-reset-token-for-locked-user'
    policy_db.execute(text('INSERT INTO password_reset_tokens(user_id,token,expires_at,used) VALUES(1,:t,:e,0)'),
                      {'t': reset_token_digest(reset), 'e': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    policy_db.commit()
    response = auth_client.post('/api/v1/auth/reset-password', json={'token': reset, 'new_password': PASSWORD + 'New'})
    assert response.status_code == 200, response.text
    assert 'access_token' not in response.json() and 'set-cookie' not in response.headers
    assert auth_client.post('/api/v1/auth/login', data={'username': 'student', 'password': PASSWORD + 'New'}).status_code == 403


def test_ws_mint_refuses_locked_user(auth_client):
    assert auth_client.post('/api/v1/auth/ws-token', headers={'Authorization': 'Bearer ' + token()}).status_code == 403


def test_live_clock_quarantine_uses_monotonic_lease_without_expiry(policy_db, tier_license, clock_env, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new(); monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('enterprise', max_users=1)
    status(policy_db)
    wall[0] += timedelta(days=365)
    assert lm.get_license().verification_state == 'verification_unavailable'
    p = status(policy_db)
    assert p['entitlement']['enforce'] and p['entitlement']['reason'] == 'uncertainty_lease'
    end = policy._read(policy_db, policy.EVIDENCE_KEY)['uncertainty_deadline']
    tick[0] += 86400
    p = status(policy_db)
    assert not p['entitlement']['enforce']
    assert policy._read(policy_db, policy.EVIDENCE_KEY)['uncertainty_deadline'] == end
    assert store.read() == BASE


def test_support_bundle_excludes_signed_envelope_and_private_selection(policy_db, monkeypatch):
    from modules.system.routes_admin import download_support_bundle
    from modules.system import diagnostics
    import subprocess, zipfile
    from io import BytesIO
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: 'owned-test' if kwargs.get('text') else b'owned-test')
    monkeypatch.setattr(diagnostics, '_read_tail', lambda *args, **kwargs: [])
    response = download_support_bundle(user={'id': 3, 'role': 'admin', 'group_id': None}, db=policy_db)
    async def body():
        return b''.join([chunk async for chunk in response.body_iterator])
    with zipfile.ZipFile(BytesIO(asyncio.run(body()))) as archive:
        settings = archive.read('settings_safe.json').decode()
    assert policy.EVIDENCE_KEY not in settings
    assert policy.POLICY_KEY not in settings
    assert 'signed_envelope' not in settings


def test_raw_cached_caps_are_not_evidence(policy_db, tier_license):
    status(policy_db)
    p = policy._read(policy_db, policy.POLICY_KEY)
    p['entitlement']['max_users'] = 0
    policy._write(policy_db, policy.POLICY_KEY, p); policy_db.commit()
    tier_license(None)
    p = status(policy_db)
    assert not p['entitlement']['enforce']
    assert p['warning'] == 'license_policy_evidence_mismatch'


def test_simultaneous_recovery_demotion_cannot_remove_both(policy_db):
    engine = policy_db.get_bind()
    def demote(uid):
        with Session(engine) as db:
            try:
                db.execute(text("UPDATE users SET role='viewer' WHERE id=:id"), {'id': uid})
                db.commit(); return 200
            except policy.LicenseLockError as exc:
                db.rollback(); return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(demote, [3, 5])) == [200, 422]
    p = status(policy_db)
    assert len(p['users']) == 1 and p['users'][0] == p['recovery_user_id']


def test_http_license_upload_recomputes_and_explicit_removal_records_intent(policy_db, auth_client, tier_license):
    import license_manager as lm
    from pathlib import Path
    tier_license('enterprise', max_users=None, max_printers=None)
    replacement = (Path(lm.LICENSE_DIR) / lm.LICENSE_FILENAME).read_bytes()
    tier_license('enterprise', max_users=1, max_printers=1)
    before = status(policy_db)
    headers = {'Authorization': 'Bearer ' + token('admin')}
    result = auth_client.post('/api/v1/license/upload', headers=headers, files={'file': ('odin.license', replacement)})
    assert result.status_code == 200, result.text
    p = policy._read(policy_db, policy.POLICY_KEY)
    assert p['revision'] > before['revision'] and len(p['users']) == 5
    assert auth_client.get('/api/v1/private', headers={'Authorization': 'Bearer ' + token()}).status_code == 200
    assert auth_client.delete('/api/v1/license', headers=headers).status_code == 200
    p = status(policy_db)
    assert p['entitlement']['state'] == 'community_selected'
    assert not p['entitlement']['enforce']  # downgrade decision remains blocked


def test_stalled_stored_media_transfer_closes_when_owner_locks(policy_db, tmp_path):
    import os
    from core.timelapse_fd_response import AdmittedFDResponse
    path = tmp_path / 'owned-media.mp4'; path.write_bytes(b'owned-media')
    fd = os.open(path, os.O_RDONLY)
    engine = policy_db.get_bind()
    def authorize():
        with Session(engine) as db: policy.authorize_user(db, 3)
    response = AdmittedFDResponse(fd, size=11, authorize=authorize)
    async def run():
        started = asyncio.Event()
        async def receive(): await asyncio.Event().wait()
        async def send(message):
            if message['type'] == 'http.response.body':
                started.set()
                await asyncio.Event().wait()  # consumer stalls; no later frames/events
        transfer = asyncio.create_task(response({}, receive, send))
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.to_thread(pick, policy_db, [5], recovery=5)
        with pytest.raises(policy.LicenseLockError):
            await asyncio.wait_for(transfer, 3)
    asyncio.run(run())
    assert response.fd is None
    with pytest.raises(OSError): os.fstat(fd)


def test_unlimited_users_admits_every_active_user_without_inventing_recovery(policy_db, tier_license, monkeypatch):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    policy_db.execute(text("UPDATE users SET password_hash='sso-only'")); policy_db.commit()
    tier_license('enterprise', max_users=None)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    p = status(policy_db)
    assert set(p['users']) == {1, 2, 3, 4, 5}
    assert p['recovery_user_id'] is None
    assert p['warning'] == 'local_recovery_bootstrap_required'


def test_membership_local_auth_and_physical_identity_changes_invalidate(policy_db):
    before = status(policy_db)
    policy_db.execute(text("UPDATE users SET oidc_issuer='https://synthetic.invalid',mfa_enabled=TRUE WHERE id=1"))
    policy_db.execute(text("UPDATE printers SET api_host='192.0.2.10' WHERE id=9"))
    policy_db.commit()
    after = status(policy_db)
    assert after['membership_generation'] == before['membership_generation'] + 2
    assert after['revision'] > before['revision']


def test_http_recovery_handoff_is_one_atomic_seat(auth_client, policy_db):
    p = status(policy_db)
    old_headers = {'Authorization': 'Bearer ' + token('admin')}
    result = auth_client.put('/api/v1/license/selection', headers=old_headers, json={
        'printers': p['printers'], 'users': [5], 'expected_revision': p['revision'], 'recovery_user_id': 5})
    assert result.status_code == 200, result.text
    assert result.json()['users'] == [5] and result.json()['recovery_user_id'] == 5
    assert auth_client.get('/api/v1/license/selection', headers=old_headers).status_code == 403
    assert auth_client.get('/api/v1/license/selection', headers={'Authorization': 'Bearer ' + token('foreign')}).status_code == 200
