"""Second-pass regressions, including a frozen Slice A compatibility oracle."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core import license_lock as policy
from tests.test_contracts.test_license_clock import clock_env
from tests.test_contracts.test_license_policy_b1 import policy_db, status, token, pick, auth_client, PASSWORD
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_roster_auth import client, google_double, callback_url
from tests.test_contracts.test_roster_sync import setup, imported


@pytest.fixture
def verification_pair(monkeypatch, clock_env):
    import license_manager as lm
    path = Path(__file__).parents[1] / 'fixtures/license_slice_a_verifier.py'
    spec = importlib.util.spec_from_file_location('slice_a_oracle', path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    for flag in ('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'ODIN_LICENSE_PAID_LAPSE_ENABLED', 'ODIN_REQUIRE_LICENSE_BINDING'):
        monkeypatch.delenv(flag, raising=False)
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(lm, 'ODIN_PUBLIC_KEY', key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    # Separate fresh clocks, same deterministic time and evidence.
    monkeypatch.setattr(lm, 'license_clock', clock_env[3])
    monkeypatch.setattr(old, 'license_clock', clock_env[3])
    def compare(payload):
        raw = json.dumps(payload).encode()
        wire = base64.urlsafe_b64encode(raw).decode() + '.' + base64.urlsafe_b64encode(key.sign(raw)).decode()
        kwargs = dict(installation_id_reader=lambda: 'owned-install', device_pubkey_reader=lambda: None)
        return old.verify_license_text(wire, **kwargs), lm.verify_license_text(wire, **kwargs)
    return compare


# Shapes copied from signed_license in test_license_user_limits, the portal-null
# tests in test_license_tiers_unlimited_and_defaults, and test_trial_license.
SHAPES = [
    {}, {'email': None}, {'key': None}, {'installation_id': None},
    {'email': None, 'key': None, 'installation_id': None, 'licensee': None},
    {'features': None, 'max_users': None, 'max_printers': None},
    {'features': []}, {'features': ['education_workflows', 'custom_school_feature']},
    {'trial': True, 'installation_id': 'owned-install'},
    {'issued_at': None}, {'issued_at': ''},
    {'schema_version': 8}, {'max_users': True}, {'max_printers': -1},
    {'features': {}}, {'issued_at': '2100-01-01'},
]


@pytest.mark.parametrize('shape', SHAPES, ids=[f'legacy-shape-{i}' for i in range(len(SHAPES))])
@pytest.mark.parametrize('tier', ['community', 'pro', 'education', 'enterprise', 'unknown'])
@pytest.mark.parametrize('expiry', ['2099-01-01', '2000-01-01', '2026-11-08T00:00:00Z'])
def test_flags_off_match_slice_a(verification_pair, shape, tier, expiry):
    old, current = verification_pair({'tier': tier, 'licensee': 'Test School', 'expires_at': expiry, **shape})
    for field in ('valid', 'expired', 'tier', 'features', 'max_users', 'max_printers'):
        assert getattr(current, field) == getattr(old, field), field


def test_historical_repository_test_license_shape(verification_pair):
    # The historical signed test artifact predates B1. Re-sign its exact payload
    # under an ephemeral test key; never install or log the original envelope.
    wire = (Path(__file__).parents[1] / 'fixtures/odin_test.license').read_text().strip()
    encoded = wire.split('.')[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    for expiry in ('2099-01-01', '2000-01-01'):
        old, current = verification_pair(dict(payload, expires_at=expiry))
        assert old.valid == (expiry == '2099-01-01')
        for field in ('valid', 'expired', 'tier', 'features'):
            assert getattr(current, field) == getattr(old, field)


@pytest.mark.parametrize('expiry', ['2099-01-01', '2000-01-01'])
def test_absent_licensee_is_explicit_compatibility_extension(verification_pair, expiry):
    # Slice A's required-field list contradicted its later .get fallback.
    # Shane explicitly requests absence be allowed, so compare to empty licensee.
    _, current = verification_pair({'tier': 'pro', 'expires_at': expiry})
    old, _ = verification_pair({'tier': 'pro', 'licensee': '', 'expires_at': expiry})
    for field in ('valid', 'expired', 'tier', 'features'):
        assert getattr(current, field) == getattr(old, field)


def test_roster_provider_failure_burns_state(setup, client, monkeypatch):
    from modules.organizations import education_roster_providers as providers
    connection, _ = imported(setup)
    calls = []
    async def failed(*args):
        calls.append(1)
        providers.refuse('roster_provider_unavailable', 503)
    monkeypatch.setattr(providers, 'google_callback', failed)
    url = callback_url(client, connection)
    assert client.get(url).status_code == 503
    assert client.get(url).status_code == 409
    assert calls == [1]


def test_roster_concurrent_callbacks_exchange_only_once(setup, client, monkeypatch):
    from core.db import get_db
    from modules.organizations import education_roster_providers as providers
    connection, _ = imported(setup)
    url = callback_url(client, connection)
    engine = setup[0].get_bind()
    def request_db():
        with Session(engine) as db:
            yield db
    client.app.dependency_overrides[get_db] = request_db
    barrier = threading.Barrier(2)
    exchanged = []
    async def provider_callback(*args):
        exchanged.append(1)
        await asyncio.sleep(0.02)
        return 'google-student', {}
    monkeypatch.setattr(providers, 'google_callback', provider_callback)
    def callback():
        barrier.wait()
        return client.get(url, follow_redirects=False).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: callback(), range(2)))
    assert sorted(results) == [302, 409]
    assert exchanged == [1]


def test_exchange_binding_mismatch_burns_receipt(setup, client, monkeypatch):
    from urllib.parse import urlsplit, parse_qs
    connection, _ = imported(setup)
    google_double(monkeypatch)
    response = client.get(callback_url(client, connection), follow_redirects=False)
    code = parse_qs(urlsplit(response.headers['location']).fragment)['roster_code'][0]
    binding = client.cookies.get('odin_roster_bind')
    client.cookies.clear()
    url = f"/api/auth/classroom/{connection['id']}/exchange"
    assert client.post(url, json={'code': code}).status_code == 403
    client.cookies.set('odin_roster_bind', binding)
    assert client.post(url, json={'code': code}).status_code == 409


@pytest.mark.parametrize('on', [False, True])
def test_principal_without_db_cannot_bypass_policy(policy_db, monkeypatch, on):
    from core import dependencies, db as database
    row = policy_db.execute(text('SELECT * FROM users WHERE id=1')).first()
    if not on:
        monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
        monkeypatch.setattr(database, 'SessionLocal', lambda: pytest.fail('flags off opened a session'))
        assert dependencies._principal(row, 'test')['id'] == 1
    else:
        with pytest.raises(policy.LicenseLockError) as exc:
            dependencies._principal(row, 'test')
        assert exc.value.status_code == 403
        monkeypatch.setattr(database, 'SessionLocal', lambda: (_ for _ in ()).throw(RuntimeError()))
        with pytest.raises(policy.LicenseLockError) as exc:
            dependencies._principal(row, 'test')
        assert exc.value.status_code == 503


@pytest.mark.parametrize('carrier', ['query', 'none'])
@pytest.mark.parametrize('on', [False, True])
def test_gate_defers_unknown_carriers(policy_db, monkeypatch, carrier, on):
    from core import dependencies
    from core.app import _register_http_middleware
    from core.config import settings
    monkeypatch.setattr(settings, 'api_key', '')
    if not on:
        monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    monkeypatch.setattr(dependencies, 'resolve_current_user_sync', lambda *a, **kw: pytest.fail('gate resolved unfamiliar carrier'))
    app = FastAPI()
    @app.get('/api/v1/media')
    def media(request: Request):
        if carrier == 'none':
            return {'ok': True}
        return dependencies.validate_access_token(token('student'), policy_db)
    _register_http_middleware(app)
    query = '?token=owned' if carrier == 'query' else ''
    response = TestClient(app).get('/api/v1/media' + query)
    assert response.status_code == (403 if on and carrier != 'none' else 200)


@pytest.mark.parametrize('carrier', ['obs', 'api_key', 'query'])
@pytest.mark.parametrize('locked', [False, True])
def test_gate_defers_to_real_carrier_authority(policy_db, monkeypatch, carrier, locked):
    from core import dependencies
    from core.auth import hash_password
    from core.app import _register_http_middleware
    from core.config import settings
    from modules.printers.camera_routes import overlay_camera_principal
    from modules.printers.models import Printer
    if locked:
        pick(policy_db, [5], recovery=5)
    credential = 'odin_camera-owned-round2'
    policy_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'OBS',:hash,:prefix,'[\"read:cameras\"]')"),
                      {'hash': hash_password(credential), 'prefix': credential[:10]})
    policy_db.execute(text("UPDATE printers SET camera_enabled=TRUE,camera_url='rtsp://192.0.2.1/owned' WHERE id=9"))
    policy_db.commit()
    monkeypatch.setenv('API_KEY', 'owned-global-key')
    monkeypatch.setattr(settings, 'api_key', '')
    monkeypatch.setattr(dependencies, 'resolve_current_user_sync', lambda *a, **kw: pytest.fail('gate preempted carrier authority'))
    app = FastAPI()
    @app.get('/api/v1/owned-carrier')
    def carrier_route(request: Request):
        if carrier == 'obs':
            principal = overlay_camera_principal(policy_db, request.query_params['token'], policy_db.get(Printer, 9))
        elif carrier == 'api_key':
            principal = dependencies.resolve_legacy_api_key(request.headers['x-api-key'], policy_db)
        else:
            principal = dependencies.validate_access_token(request.query_params['token'], policy_db)
        return {'id': principal['id']}
    _register_http_middleware(app)
    query = {} if carrier == 'api_key' else {'token': credential if carrier == 'obs' else token('admin')}
    headers = {'X-API-Key': 'owned-global-key'} if carrier == 'api_key' else {}
    # Only route-specific credentials are present; the perimeter defers them.
    http = TestClient(app)
    response = http.get('/api/v1/owned-carrier', params=query, headers=headers)
    assert response.status_code == (403 if locked else 200), response.text
    if locked:
        assert response.json()['detail']['code'] == 'license_user_locked'
    else:
        assert response.json()['id'] == 3


@pytest.mark.parametrize('on', [False, True])
def test_gate_flag_off_no_resolution_and_public_prefixes_exact(policy_db, monkeypatch, on):
    from core import dependencies
    from core.app import _register_http_middleware
    from core.config import settings
    monkeypatch.setattr(settings, 'api_key', '')
    if not on:
        monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
        monkeypatch.setattr(dependencies, 'resolve_current_user_sync', lambda *a, **kw: pytest.fail('flag off resolved principal'))
    app = FastAPI()
    for path in ('/api/v1/healthfoo', '/api/v1/setupfoo'):
        app.add_api_route(path, lambda: {'ok': True})
    _register_http_middleware(app)
    for path in ('/api/v1/healthfoo', '/api/v1/setupfoo'):
        response = TestClient(app).get(path, headers={'Authorization': 'Bearer ' + token('student')})
        assert response.status_code == (403 if on else 200)


def test_websocket_failure_uses_individual_decisions(policy_db, monkeypatch):
    from core.app_websocket import ConnectionManager
    from core import controlled_events
    manager = ConnectionManager()
    class Socket:
        async def send_json(self, message): self.message = message
        async def close(self, **kwargs): self.closed = kwargs
    sockets = [Socket() for _ in range(3)]
    principals = [{'id': 3, '_license_allowed': True}, {'id': 1, '_license_allowed': False}, {'id': 5}]
    manager.active = list(zip(sockets, principals))
    monkeypatch.setattr(controlled_events, 'audience_messages', lambda ps, msg: [msg] * len(ps))
    monkeypatch.setattr(manager, 'policy_snapshot', lambda **kw: (_ for _ in ()).throw(RuntimeError()))
    assert asyncio.run(manager.broadcast({'type': 'test'})) == 1
    assert sockets[1].closed['code'] == 4403
    assert sockets[2].closed['code'] == 1013
    assert manager.active == [(sockets[0], principals[0])]
    with pytest.raises(policy.LicenseLockError) as exc:
        manager.principal_allowed({'id': 3})
    assert exc.value.status_code == 503


def test_websocket_never_admits_from_cached_selection(policy_db):
    from core.app_websocket import ConnectionManager
    manager = ConnectionManager()
    assert manager.principal_allowed({'id': 3})
    pick(policy_db, [5], recovery=5)
    assert not manager.principal_allowed({'id': 3}, cached=True)


def test_zero_seat_recovery_can_login_and_replace_license(policy_db, tier_license, auth_client):
    import license_manager as lm
    tier_license('enterprise', max_users=None)
    replacement = (Path(lm.LICENSE_DIR) / lm.LICENSE_FILENAME).read_bytes()
    tier_license('enterprise', max_users=0)
    p = status(policy_db)
    assert p['users'] == [] and p['recovery_user_id'] == 3
    assert not policy.RESTRICTED_RECOVERY_ENABLED
    denied = auth_client.post('/api/v1/auth/login', data={'username': 'student', 'password': PASSWORD})
    assert denied.status_code == 403 and denied.json()['detail']['code'] == 'license_user_locked'
    login = auth_client.post('/api/v1/auth/login', data={'username': 'admin', 'password': PASSWORD})
    assert login.status_code == 200
    assert auth_client.get('/api/v1/private').status_code == 200
    response = auth_client.post('/api/v1/license/upload', files={'file': ('odin.license', replacement)})
    assert response.status_code == 200, response.text
    auth_client.cookies.clear()
    assert auth_client.get('/api/v1/private', headers={'Authorization': 'Bearer ' + token('student')}).status_code == 200
