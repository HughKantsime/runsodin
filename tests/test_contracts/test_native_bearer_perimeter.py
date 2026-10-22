"""Cookie-free native sign-in through the actual configured-key perimeter."""
import secrets
import uuid
import pytest
from tests.test_contracts.test_cert_F_00_live import LiveOdin

@pytest.fixture(scope='module')
def keyed_native(tmp_path_factory):
    server = LiveOdin(tmp_path_factory.mktemp('native_bearer'), env={'API_KEY': secrets.token_hex(24)}).start()
    server.setup_admin()
    yield server
    server.stop()

def test_password_and_rolling_tokens_reach_protected_routes_without_cookies(keyed_native):
    server = keyed_native
    response = server.login(*server.admin)
    assert response.status_code == 200
    bearer = {'Authorization': 'Bearer ' + response.json()['access_token']}
    # LiveOdin.get uses requests.get, not a cookie-retaining Session.
    for path in ['/api/v1/printers', '/api/v1/alerts/unread-count']:
        status = server.get(path, headers=bearer).status_code
        assert status == 200
    device = str(uuid.uuid4())
    enrolled = server.post('/api/v1/auth/biometric-token', params={'device_id': device}, headers=bearer)
    assert enrolled.status_code == 200
    renewed = server.post('/api/v1/auth/biometric-refresh', json={'device_id': device, 'refresh_token': enrolled.json()['refresh_token']})
    assert renewed.status_code == 200
    bearer = {'Authorization': 'Bearer ' + renewed.json()['access_token']}
    status = server.get('/api/v1/printers', headers=bearer).status_code
    assert status == 200

@pytest.mark.parametrize('flag', ['ws', 'mfa_pending', 'mfa_setup_required', 'expired', 'wrong-signature'])
def test_special_or_invalid_bearer_is_not_admitted(keyed_native, flag):
    import jwt
    import time
    server = keyed_native
    payload = jwt.decode(server.admin_token, server.jwt_secret, algorithms=['HS256'])
    if flag == 'expired':
        payload['exp'] = int(time.time()) - 60
    elif flag != 'wrong-signature':
        payload[flag] = True
    signing_key = secrets.token_hex(32) if flag == 'wrong-signature' else server.jwt_secret
    token = jwt.encode(payload, signing_key, algorithm='HS256')
    status = server.get('/api/v1/printers', headers={'Authorization': 'Bearer ' + token}).status_code
    assert status == 401

def test_wrong_api_key_is_not_rescued_by_valid_bearer(keyed_native):
    server = keyed_native
    status = server.get('/api/v1/printers', headers={**server.admin_headers(), 'X-API-Key': secrets.token_hex(24)}).status_code
    assert status == 401

def test_route_permissions_remain_enforced_after_bearer_admission(keyed_native):
    server = keyed_native
    username, password, _ = server.create_user('nativeviewer', 'viewer')
    bearer = server.bearer(username, password)
    status = server.get('/api/v1/printers', headers=bearer).status_code
    assert status == 200
    status = server.get('/api/config/ip-allowlist', headers=bearer).status_code
    assert status == 403

def test_revoked_or_inactive_principal_is_refused(keyed_native):
    import jwt
    from sqlalchemy import text
    server = keyed_native
    bearer = server.admin_headers()
    payload = jwt.decode(bearer['Authorization'][7:], server.jwt_secret, algorithms=['HS256'])
    with server.engine.begin() as connection:
        connection.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:exp)'), {'jti':payload['jti'],'exp':'2099-01-01'})
    status = server.get('/api/v1/printers', headers=bearer).status_code
    assert status == 401
    with server.engine.begin() as connection:
        connection.execute(text('DELETE FROM token_blacklist WHERE jti=:jti'), {'jti':payload['jti']})
        connection.execute(text('UPDATE users SET is_active=0 WHERE username=:name'), {'name':server.admin[0]})
    try:
        status = server.get('/api/v1/printers', headers=bearer).status_code
        assert status == 401
    finally:
        with server.engine.begin() as connection:
            connection.execute(text('UPDATE users SET is_active=1 WHERE username=:name'), {'name':server.admin[0]})


@pytest.fixture
def sentinel(tmp_path, monkeypatch):
    """Real middleware/validator, but no route auth that could mask admission."""
    import threading
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from core.app import _register_http_middleware
    from core.config import settings
    from core import auth, db

    engine = create_engine('sqlite:///' + str(tmp_path / 'sentinel.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER, username TEXT, role TEXT, is_active INTEGER, created_at TEXT)'))
        conn.execute(text("INSERT INTO users VALUES (1, 'sentinel', 'admin', 1, '2000-01-01')"))
        conn.execute(text('CREATE TABLE token_blacklist (jti TEXT)'))
    events = []

    class TrackedSession(Session):
        def __init__(self):
            events.append(('create', threading.get_ident()))
            super().__init__(bind=engine)

        def execute(self, *args, **kwargs):
            events.append(('execute', threading.get_ident()))
            return super().execute(*args, **kwargs)

        def close(self):
            events.append(('close', threading.get_ident()))
            super().close()

    monkeypatch.setattr(settings, 'api_key', 'sentinel-key')
    monkeypatch.setattr(db, 'SessionLocal', TrackedSession)
    # Allowlist has its own session; these tests isolate bearer session tracking.
    monkeypatch.setattr('core.app._ip_allowlist_denial', lambda *_: None)
    app = FastAPI()

    @app.get('/api/v1/perimeter-sentinel')
    async def admitted():
        events.append(('route', threading.get_ident()))
        return {'admitted': True}

    _register_http_middleware(app)
    token = auth.create_access_token({'sub': 'sentinel', 'role': 'admin'})
    with TestClient(app) as client:
        yield client, token, engine, events
    engine.dispose()


def _sentinel_get(client, token):
    return client.get('/api/v1/perimeter-sentinel', headers={'Authorization': 'Bearer ' + token})


@pytest.mark.parametrize('case', [
    'refresh', 'biometric', 'scoped', 'api', 'wrong-type', 'wrong-purpose',
    'account-purpose', 'wrong-audience', 'expired', 'wrong-signature',
    'missing-exp', 'missing-iat', 'missing-jti', 'empty-jti', 'invalid-jti',
    'invalid-exp', 'invalid-iat', 'future-iat', 'reversed-times',
    'ws', 'mfa_pending', 'mfa_setup_required', 'revoked', 'inactive',
])
def test_sentinel_rejects_non_access_credentials(sentinel, case):
    import jwt
    import time
    from sqlalchemy import text
    from core import auth
    client, token, engine, events = sentinel
    payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    if case.startswith('missing-'):
        del payload[case[8:]]
    elif case in ('refresh', 'biometric', 'api', 'wrong-type'):
        payload['type'] = case
    elif case == 'scoped':
        payload['scopes'] = ['read']
    elif case == 'wrong-purpose':
        payload['purpose'] = 'refresh'
    elif case == 'account-purpose':
        payload['account_purpose'] = 'access'
    elif case == 'wrong-audience':
        payload['aud'] = 'other-service'
    elif case == 'expired':
        payload['exp'] = int(time.time()) - 60
    elif case == 'future-iat':
        payload['iat'] = int(time.time()) + 3600
    elif case == 'reversed-times':
        payload['exp'] = payload['iat']
    elif case == 'empty-jti':
        payload['jti'] = ''
    elif case == 'invalid-jti':
        payload['jti'] = 123
    elif case in ('invalid-exp', 'invalid-iat'):
        payload[case[8:]] = 'invalid'
    elif case in ('ws', 'mfa_pending', 'mfa_setup_required'):
        payload[case] = True
    elif case == 'revoked':
        with engine.begin() as conn:
            conn.execute(text('INSERT INTO token_blacklist VALUES (:jti)'), {'jti': payload['jti']})
    elif case == 'inactive':
        with engine.begin() as conn:
            conn.execute(text('UPDATE users SET is_active=0'))
    key = 'wrong-signature-key-for-sentinel-test' if case == 'wrong-signature' else auth.SECRET_KEY
    token = jwt.encode(payload, key, algorithm=auth.ALGORITHM)
    assert _sentinel_get(client, token).status_code == 401
    assert 'route' not in [name for name, _ in events]
    assert [name for name, _ in events].count('close') == 1


def test_sentinel_admits_full_access_and_session_lifetime_is_in_worker(sentinel):
    client, token, _, events = sentinel
    assert _sentinel_get(client, token).json() == {'admitted': True}
    workers = {thread for name, thread in events if name != 'route'}
    assert len(workers) == 1
    assert workers.isdisjoint({thread for name, thread in events if name == 'route'})
    assert events[-2][0] == 'close'
    assert events[-1][0] == 'route'


@pytest.mark.parametrize('denial', [403, 503])
def test_allowlist_precedes_bearer_validation(sentinel, monkeypatch, denial):
    client, token, _, events = sentinel
    monkeypatch.setattr('core.app._ip_allowlist_denial', lambda *_: denial)
    assert _sentinel_get(client, token).status_code == denial
    assert events == []


def test_validation_exception_fails_closed_and_closes_session(sentinel, monkeypatch):
    client, token, _, events = sentinel

    def unavailable(*_):
        raise RuntimeError('validation unavailable')

    monkeypatch.setattr('core.dependencies.validate_perimeter_access_token', unavailable)
    assert _sentinel_get(client, token).status_code == 503
    assert [name for name, _ in events] == ['create', 'close']
    assert events[0][1] == events[1][1]


def test_scoped_opaque_api_token_is_not_a_bearer_access_token(sentinel):
    client, _, _, events = sentinel
    assert _sentinel_get(client, 'odin_' + secrets.token_hex(24)).status_code == 401
    assert [name for name, _ in events] == ['create', 'close']


@pytest.mark.parametrize('stage', ['create', 'close'])
def test_session_lifecycle_exception_fails_closed(sentinel, monkeypatch, stage):
    from core import db
    client, token, _, events = sentinel
    original = db.SessionLocal

    def factory():
        if stage == 'create':
            raise RuntimeError('session unavailable')
        session = original()
        close = session.close

        def failing_close():
            close()
            raise RuntimeError('close unavailable')

        session.close = failing_close
        return session

    monkeypatch.setattr(db, 'SessionLocal', factory)
    assert _sentinel_get(client, token).status_code == 503
    assert 'route' not in [name for name, _ in events]
    if stage == 'close':
        assert [name for name, _ in events].count('close') == 1
