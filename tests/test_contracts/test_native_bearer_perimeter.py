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
