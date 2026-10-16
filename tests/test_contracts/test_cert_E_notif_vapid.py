"""Review H1: VAPID private key encrypted at rest, env override precedence, rotation."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_channels import dispatch, notif_env, receiver, review_db, signed_license  # noqa: F401
from tests.test_contracts.test_cert_E_notif_push import b64u, browser, subscribe, uncompressed, verify_vapid_jwt  # noqa: F401


@pytest.fixture
def push_app(notif_env):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications.routes import providers
    app = FastAPI()
    app.include_router(providers.router)
    identity = {'id': 1, 'username': 'student', 'role': 'viewer', 'group_id': 1}

    def session():
        with Session(notif_env.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: identity
    with TestClient(app) as client:
        yield client, identity


def _stored(db):
    value = db.execute(text("SELECT value FROM system_config WHERE key='vapid_keys'")).scalar_one()
    return json.loads(value) if isinstance(value, str) else value


def test_generated_private_key_is_encrypted_at_rest(push_app, notif_env):
    import core.crypto as crypto
    client, _ = push_app
    public = client.get('/push/vapid-key').json()['public_key']
    stored = _stored(notif_env)
    assert stored['public_key'] == public
    assert crypto.is_encrypted(stored['private_key'])
    raw = crypto.decrypt(stored['private_key'])
    derived = ec.derive_private_key(int.from_bytes(__import__('base64').urlsafe_b64decode(raw + '=' * (-len(raw) % 4)), 'big'),
                                    ec.SECP256R1()).public_key()
    assert b64u(uncompressed(derived)) == public


def test_legacy_plaintext_private_key_still_signs(push_app, notif_env, receiver, browser):
    """Rows written before encryption are read transparently."""
    client, _ = push_app
    private = ec.generate_private_key(ec.SECP256R1())
    legacy = {'private_key': b64u(private.private_numbers().private_value.to_bytes(32, 'big')),
              'public_key': b64u(uncompressed(private.public_key()))}
    notif_env.execute(text("INSERT INTO system_config(key,value) VALUES('vapid_keys',:v)"), {'v': json.dumps(legacy)})
    notif_env.commit()
    assert client.get('/push/vapid-key').json() == {'public_key': legacy['public_key'], 'enabled': True}
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    receiver.status = 201
    subscribe(client, receiver, browser)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, browser_push=True, email=False))
    notif_env.commit()
    dispatch('daemon', 'print_failed', 'critical', 'Legacy key push', 'b', notif_env)
    request, = receiver.wait(1)
    verify_vapid_jwt(request['headers']['authorization'], legacy['public_key'], f'http://127.0.0.1:{receiver.port}')


@pytest.mark.parametrize('path', ['daemon', 'orm'])
def test_env_override_wins_for_serving_and_signing(push_app, notif_env, receiver, browser, monkeypatch, path):
    client, _ = push_app
    client.get('/push/vapid-key')  # a stored pair exists too
    env_private = ec.generate_private_key(ec.SECP256R1())
    env_public = b64u(uncompressed(env_private.public_key()))
    monkeypatch.setenv('VAPID_PRIVATE_KEY', b64u(env_private.private_numbers().private_value.to_bytes(32, 'big')))
    assert client.get('/push/vapid-key').json() == {'public_key': env_public, 'enabled': True}
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    receiver.status = 201
    subscribe(client, receiver, browser)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, browser_push=True, email=False))
    notif_env.commit()
    dispatch(path, 'print_failed', 'critical', 'Env key push', 'b', notif_env)
    request, = receiver.wait(1, timeout=3)
    verify_vapid_jwt(request['headers']['authorization'], env_public, f'http://127.0.0.1:{receiver.port}')
    # Rotation is refused while the override is set.
    client_identity = push_app[1]
    client_identity.update(role='admin', group_id=None)
    assert client.post('/push/vapid-key/rotate').status_code == 409


def test_underivable_env_key_without_public_key_disables_serving(push_app, monkeypatch):
    client, _ = push_app
    monkeypatch.setenv('VAPID_PRIVATE_KEY', 'not-a-key')
    monkeypatch.delenv('VAPID_PUBLIC_KEY', raising=False)
    assert client.get('/push/vapid-key').json() == {'public_key': None, 'enabled': False}
    monkeypatch.setenv('VAPID_PUBLIC_KEY', 'BExplicitPublic')
    assert client.get('/push/vapid-key').json() == {'public_key': 'BExplicitPublic', 'enabled': True}


def test_rotation_replaces_pair_invalidates_subscriptions_and_is_superadmin_only(push_app, notif_env, receiver, browser):
    import core.crypto as crypto
    client, identity = push_app
    old = client.get('/push/vapid-key').json()['public_key']
    subscribe(client, receiver, browser)
    assert notif_env.execute(text('SELECT COUNT(*) FROM push_subscriptions')).scalar_one() == 1
    identity.update(role='admin', group_id=1)  # org admin on a multi-tenant install
    assert client.post('/push/vapid-key/rotate').status_code == 403
    identity.update(role='admin', group_id=None)
    rotated = client.post('/push/vapid-key/rotate')
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body['public_key'] != old and body['subscriptions_invalidated'] == 1
    with Session(notif_env.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM push_subscriptions')).scalar_one() == 0
        stored = _stored(fresh)
        assert stored['public_key'] == body['public_key'] and crypto.is_encrypted(stored['private_key'])
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='push.vapid_rotated'")).scalar_one() == 1
    assert client.get('/push/vapid-key').json()['public_key'] == body['public_key']
