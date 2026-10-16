"""Cert-E F13-13.1-01 Browser Push (Web Push + VAPID) at the push-service boundary.

The browser vendor's push service is replaced by a real HTTP server on
127.0.0.1; the production pywebpush path performs the real VAPID signing and
RFC 8291 aes128gcm payload encryption. The receiving "browser" keypair and auth
secret are generated in the test, so the captured request is verified the way a
push service and user agent would: JWT ES256 signature against the server's
published applicationServerKey, audience = push-service origin, and decryption of
the body with the subscription private key.
"""
import base64
import json
import os

import http_ece
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_channels import (  # noqa: F401
    Receiver, dispatch, notif_env, receiver, review_db, signed_license)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def uncompressed(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


@pytest.fixture
def vapid(notif_env):
    """Generate a VAPID keypair locally (no external key service) and store it
    in the only place ODIN reads it from: system_config['vapid_keys']."""
    private = ec.generate_private_key(ec.SECP256R1())
    keys = {'private_key': b64u(private.private_numbers().private_value.to_bytes(32, 'big')),
            'public_key': b64u(uncompressed(private.public_key()))}
    notif_env.execute(text("INSERT INTO system_config(key,value) VALUES('vapid_keys',:v)"), {'v': json.dumps(keys)})
    notif_env.commit()
    return keys


@pytest.fixture
def browser():
    """A synthetic user agent: P-256 ECDH keypair + 16-byte auth secret."""
    private = ec.generate_private_key(ec.SECP256R1())
    return {'private': private, 'p256dh': b64u(uncompressed(private.public_key())), 'auth_raw': os.urandom(16)}


@pytest.fixture
def push_api(notif_env):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications.routes import providers
    app = FastAPI()
    app.include_router(providers.router)

    def session():
        with Session(notif_env.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'username': 'student', 'role': 'viewer', 'group_id': 1}
    with TestClient(app) as client:
        yield client


def subscribe(push_api, receiver, browser, name='sub-1'):
    endpoint = f'http://127.0.0.1:{receiver.port}/push/{name}'
    response = push_api.post('/push/subscribe', json={
        'endpoint': endpoint, 'p256dh_key': browser['p256dh'], 'auth_key': b64u(browser['auth_raw'])})
    assert response.status_code == 200, response.text
    return endpoint


def verify_vapid_jwt(authorization: str, published_public_key: str, expected_aud: str) -> dict:
    scheme, params = authorization.split(' ', 1)
    assert scheme == 'vapid'
    fields = dict(part.strip().split('=', 1) for part in params.split(','))
    assert fields['k'] == published_public_key  # signed with the key the browser subscribed to
    header_b64, claims_b64, signature_b64 = fields['t'].split('.')
    assert json.loads(unb64u(header_b64)) == {'typ': 'JWT', 'alg': 'ES256'}
    raw = unb64u(signature_b64)
    assert len(raw) == 64
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(published_public_key))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
                  f'{header_b64}.{claims_b64}'.encode(), ec.ECDSA(hashes.SHA256()))
    claims = json.loads(unb64u(claims_b64))
    assert claims['aud'] == expected_aud and claims['sub'].startswith('mailto:')
    return claims


def test_vapid_public_key_endpoint_serves_locally_stored_key(push_api, vapid):
    assert push_api.get('/push/vapid-key').json() == {'public_key': vapid['public_key'], 'enabled': True}


def test_daemon_alert_push_is_vapid_signed_and_decryptable_by_subscriber(
        push_api, notif_env, receiver, vapid, browser):
    import time
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    receiver.status = 201
    endpoint = subscribe(push_api, receiver, browser)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, browser_push=True, email=False))
    notif_env.commit()
    dispatch('daemon', 'print_failed', 'critical', 'Synthetic push title', 'Synthetic push body', notif_env)
    request, = receiver.wait(1)
    assert request['method'] == 'POST' and request['path'] == '/push/sub-1'
    assert request['headers']['content-encoding'] == 'aes128gcm'
    assert int(request['headers']['ttl']) >= 0
    claims = verify_vapid_jwt(request['headers']['authorization'], vapid['public_key'],
                              f'http://127.0.0.1:{receiver.port}')
    assert claims['exp'] > time.time()
    plaintext = http_ece.decrypt(request['body'], private_key=browser['private'],
                                 auth_secret=browser['auth_raw'], version='aes128gcm')
    payload = json.loads(plaintext)
    alert_id = notif_env.execute(text("SELECT id FROM alerts WHERE user_id=1")).scalar_one()
    assert payload == {'title': 'Synthetic push title', 'body': 'Synthetic push body', 'alert_type': 'print_failed',
                       'alert_id': None, 'printer_id': None, 'job_id': None, 'url': '/alerts'}
    assert alert_id  # in-app record exists alongside the push
    assert endpoint.endswith('/push/sub-1')


@pytest.mark.parametrize('status,remaining', [(410, 0), (404, 0), (503, 1)])
def test_push_service_expiry_response_removes_only_expired_subscription(
        push_api, notif_env, receiver, vapid, browser, status, remaining):
    from modules.notifications import channels
    receiver.status = status
    subscribe(push_api, receiver, browser)
    assert channels.send_push_notification(1, 'print_failed', 'Synthetic', 'Synthetic') is False
    receiver.wait(1)
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM push_subscriptions')).scalar_one() == remaining


def test_orm_dispatched_alert_push_uses_served_vapid_key(push_api, notif_env, receiver, vapid, browser):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    receiver.status = 201
    subscribe(push_api, receiver, browser)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, browser_push=True, email=False))
    notif_env.commit()
    dispatch('orm', 'print_failed', 'critical', 'Synthetic push title', 'Synthetic push body', notif_env)
    request, = receiver.wait(1, timeout=3)
    verify_vapid_jwt(request['headers']['authorization'], vapid['public_key'], f'http://127.0.0.1:{receiver.port}')


def test_browser_subscription_json_shape_is_accepted(push_api, receiver, browser):
    response = push_api.post('/push/subscribe', json={
        'endpoint': f'http://127.0.0.1:{receiver.port}/push/w3c', 'expirationTime': None,
        'keys': {'p256dh': browser['p256dh'], 'auth': b64u(browser['auth_raw'])}})
    assert response.status_code == 200, response.text


def test_fresh_install_provisions_local_vapid_keys(push_api, notif_env):
    from core.schema import bootstrap_database
    from pathlib import Path
    bootstrap_database(notif_env.get_bind(), Path(__file__).resolve().parents[2] / 'backend')
    first = push_api.get('/push/vapid-key').json()
    assert first['enabled'] is True
    # The served key is a 65-byte uncompressed P-256 point, stable across requests.
    from cryptography.hazmat.primitives.asymmetric import ec
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(first['public_key']))
    assert push_api.get('/push/vapid-key').json() == first
    from modules.notifications.vapid import read_vapid_keys
    with Session(notif_env.get_bind()) as db:
        assert read_vapid_keys(db)['public_key'] == first['public_key']


def test_generated_key_signs_daemon_and_orm_pushes(push_api, notif_env, receiver, browser):
    """End to end on a fresh install: served key -> browser subscribes -> both dispatchers
    sign with the private half of that same generated pair."""
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    public = push_api.get('/push/vapid-key').json()['public_key']
    receiver.status = 201
    subscribe(push_api, receiver, browser)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, browser_push=True, email=False))
    notif_env.commit()
    for path, title in (('daemon', 'Synthetic daemon push'), ('orm', 'Synthetic orm push')):
        dispatch(path, 'print_failed', 'critical', title, 'body', notif_env)
    requests = receiver.wait(2, timeout=3)
    for request in requests:
        verify_vapid_jwt(request['headers']['authorization'], public, f'http://127.0.0.1:{receiver.port}')
    titles = {json.loads(http_ece.decrypt(r['body'], private_key=browser['private'], auth_secret=browser['auth_raw'],
                                          version='aes128gcm'))['title'] for r in requests}
    assert titles == {'Synthetic daemon push', 'Synthetic orm push'}
