"""ODIN boundary only: ephemeral signer + isolated device, no live portal."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from tests.test_contracts.test_license_offline_import import owned_import

CONTRACT = json.loads((Path(__file__).parent / 'fixtures/paid_binding_portal.json').read_text())

@pytest.fixture
def device(owned_import):
    upload, path, old, audit, db, lm, sign = owned_import
    install = lm.get_installation_id()
    _, pub = lm.get_device_keypair()
    fields = dict(installation_id=install, binding_version=1, device_pubkey=pub,
                  device_fingerprint=hashlib.sha256(base64.b64decode(pub)).hexdigest())
    return owned_import, fields


def test_valid_import_restart_and_private_diagnostics(device):
    (upload, path, old, audit, db, lm, sign), fields = device
    candidate = sign(**fields)
    upload(candidate)
    assert lm.get_license().valid and lm.load_license().device_bound
    assert lm.load_license().to_dict()['device_fingerprint'] == fields['device_fingerprint']
    public = lm.get_license().to_public_dict()
    assert public['device_bound']
    assert 'device_fingerprint' not in public and 'installation_id' not in public
    assert path.read_bytes() == candidate
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('change', [
    {'binding_version': None}, {'binding_version': True}, {'binding_version': 2},
    {'binding_version': '1'}, {'device_pubkey': None}, {'device_pubkey': 'bad'},
    {'device_pubkey': base64.b64encode(b'x'*31).decode()},
    {'device_fingerprint': '0'*64}, {'device_fingerprint': None},
    {'installation_id': None}, {'installation_id': 'another-installation'},
])
def test_malformed_or_mismatched_bound_import_keeps_entitlement(device, change):
    (upload, path, old, audit, db, lm, sign), fields = device
    with pytest.raises(HTTPException): upload(sign(**(fields | change)))
    assert path.read_text() == old and lm.load_license().valid


@pytest.mark.parametrize('field', ['binding_version', 'device_pubkey', 'device_fingerprint'])
def test_partial_binding_is_never_legacy(owned_import, field):
    upload, path, old, audit, db, lm, sign = owned_import
    assert not lm.verify_license_text(sign(**{field: 1}).decode()).valid
    assert lm.verify_license_text(old).valid


def test_noncanonical_base64_rejected(device):
    (upload, path, old, audit, db, lm, sign), fields = device
    fields['device_pubkey'] += '='
    assert not lm.verify_license_text(sign(**fields).decode()).valid


@pytest.mark.parametrize('change', ['missing', 'replaced', 'corrupt', 'installation'])
def test_identity_content_invalidates_cache_without_license_mtime_change(device, change):
    (upload, path, old, audit, db, lm, sign), fields = device
    lm.save_license_file(sign(**fields).decode())
    assert lm.get_license().valid
    mtime = path.stat().st_mtime_ns
    key = path.parent / lm.DEVICE_PRIVATE_KEY_FILENAME
    if change == 'missing': key.unlink()
    elif change == 'replaced':
        key.unlink(); lm.get_device_keypair()
    elif change == 'corrupt': key.write_bytes(b'corrupt')
    else: (path.parent / lm.INSTALL_ID_FILENAME).write_text('other')
    assert not lm.get_license().valid
    assert path.stat().st_mtime_ns == mtime
    if change == 'missing': assert not key.exists()


def test_legacy_paid_ignores_missing_and_replaced_keys(device):
    (upload, path, old, audit, db, lm, sign), fields = device
    key = path.parent / lm.DEVICE_PRIVATE_KEY_FILENAME
    key.unlink()
    assert lm.get_license().valid and not key.exists()
    lm.get_device_keypair()
    assert lm.get_license().valid and not lm.get_license().device_bound


def test_trial_restriction_and_expiry_preserved(device):
    (upload, path, old, audit, db, lm, sign), fields = device
    assert lm.verify_license_text(sign(trial=True, device_pubkey=fields['device_pubkey']).decode()).valid
    assert not lm.verify_license_text(sign(trial=True, device_pubkey='wrong').decode()).valid
    assert lm.verify_license_text(sign(**fields, expires_at='2000-01-01').decode()).expired
    raw = sign(**fields).decode()
    assert not lm.verify_license_text(raw[:-8] + 'AAAAAAAA').valid


@pytest.fixture
def portal(device, monkeypatch):
    from modules.system import routes_health as routes
    from core import itar
    (upload, path, old, audit, db, lm, sign), fields = device
    monkeypatch.setattr(routes, 'get_installation_id', lm.get_installation_id)
    monkeypatch.setattr(routes, 'get_device_keypair', lm.get_device_keypair)
    monkeypatch.setattr(itar, 'pin_for_request', lambda _: nullcontext())
    calls = []
    response = dict(status=200, body={'license_file': sign(**fields).decode()})
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            calls.append((url, json))
            return Mock(status_code=response['status'], json=lambda: response['body'])
    async def challenge(*args): return 'fresh-test-challenge'
    monkeypatch.setattr(routes, '_fetch_license_challenge', challenge)
    monkeypatch.setattr(routes.httpx, 'AsyncClient', Client)
    def activate(**kwargs):
        return asyncio.run(routes.activate_license(routes.LicenseActivateRequest(key='TEST-ONLY', **kwargs), current_user={'id':1}, license_mutation_allowed=None))
    return device, routes, calls, response, activate


def test_bound_activation_and_conversion_wire(portal):
    device, routes, calls, response, activate = portal
    (upload, path, old, audit, db, lm, sign), fields = device
    activate(**CONTRACT['conversion'])
    assert lm.get_license().device_bound
    body = calls[0][1]
    assert all(body[k] == v for k, v in CONTRACT['conversion'].items())
    assert body['nonce'] and body['bootstrap_signature']
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(body['device_pubkey'])).verify(
        base64.b64decode(body['bootstrap_signature']),
        f"odin-license-v1\nactivate-bootstrap\nTEST-ONLY\n{fields['installation_id']}\nfresh-test-challenge".encode())


@pytest.mark.parametrize('convert', [False, True])
@pytest.mark.parametrize('bad', ['legacy', 'malformed', 'mismatch', 'no_file', 'refusal'])
def test_failed_activation_and_conversion_preserve_old_file(portal, bad, convert):
    device, routes, calls, response, activate = portal
    (upload, path, old, audit, db, lm, sign), fields = device
    if bad == 'legacy': response['body'] = {'license_file': old}
    elif bad == 'malformed': response['body'] = []
    elif bad == 'mismatch': response['body'] = {'license_file': sign(**(fields | {'device_pubkey': 'bad'})).decode()}
    elif bad == 'no_file': response['body'] = {}
    else: response.update(status=409, body=CONTRACT['refusals'][0])
    with pytest.raises(HTTPException):
        activate(binding_protocol='device-v1', convert_to_device_binding=convert, confirm_purchase_wide=convert)
    assert path.read_text() == old and lm.load_license().valid


def test_bound_challenge_failure_never_posts(portal, monkeypatch):
    device, routes, calls, response, activate = portal
    async def refused(*args): raise HTTPException(502, 'unavailable')
    monkeypatch.setattr(routes, '_fetch_license_challenge', refused)
    with pytest.raises(HTTPException): activate(binding_protocol='device-v1')
    assert not calls
    assert device[0][1].read_text() == device[0][2]


def test_conversion_requires_confirmation(portal):
    device, routes, calls, response, activate = portal
    with pytest.raises(HTTPException): activate(binding_protocol='device-v1', convert_to_device_binding=True)
    assert not calls


def test_portal_postreplace_sync_failure_restores_old_license(portal, monkeypatch):
    import stat
    device, routes, calls, response, activate = portal
    (upload, path, old, audit, db, lm, sign), fields = device
    original = lm.os.fsync
    def fail(fd):
        if stat.S_ISDIR(lm.os.fstat(fd).st_mode): raise OSError('test sync failure')
        return original(fd)
    monkeypatch.setattr(lm.os, 'fsync', fail)
    with pytest.raises(HTTPException): activate(binding_protocol='device-v1')
    assert path.read_text() == old and lm.get_license().valid

@pytest.mark.parametrize('refusal', CONTRACT['refusals'])
def test_structured_portal_refusals_preserve_current_file(portal, refusal):
    device, routes, calls, response, activate = portal
    response.update(status=403, body=refusal)
    with pytest.raises(HTTPException) as exc: activate(binding_protocol='device-v1')
    assert exc.value.status_code == 502  # Portal auth never logs the ODIN user out.
    assert exc.value.detail['code'] == refusal['code']
    assert device[0][1].read_text() == device[0][2]


def test_network_failure_keeps_current_file(portal, monkeypatch):
    device, routes, calls, response, activate = portal
    def failed(**kwargs): raise OSError('test transport failure')
    monkeypatch.setattr(routes.httpx, 'AsyncClient', failed)
    with pytest.raises(HTTPException): activate(binding_protocol='device-v1')
    assert device[0][1].read_text() == device[0][2]


def test_offline_request_explicitly_carries_protocol(device):
    from modules.system import routes_health as routes
    result = routes.build_activation_request(routes.OfflineActivationRequestBody(
        key='TEST-ONLY', nonce='fresh-offline-challenge', binding_protocol='device-v1'), current_user={'id':1})
    request = json.loads(result.body)['activation_request']
    assert request['binding_protocol'] == 'device-v1'
    assert request['bootstrap_signature'] and request['nonce'] == 'fresh-offline-challenge'
    assert 'PRIVATE' not in result.body.decode()


def test_restoring_original_key_revalidates_same_file(device):
    (upload, path, old, audit, db, lm, sign), fields = device
    lm.save_license_file(sign(**fields).decode())
    key = path.parent / lm.DEVICE_PRIVATE_KEY_FILENAME
    original = key.read_bytes()
    assert lm.get_license().valid
    key.unlink()
    assert not lm.get_license().valid
    key.write_bytes(original)
    assert lm.get_license().valid


def test_bound_refresh_refuses_unbound_portal_response(portal, monkeypatch):
    device, routes, calls, response, activate = portal
    (upload, path, old, audit, db, lm, sign), fields = device
    installed = sign(**fields, key='TEST-ONLY').decode()
    lm.save_license_file(installed)
    monkeypatch.setattr(routes, 'get_license', lm.get_license)
    response['body'] = {'license_file': old}
    with pytest.raises(HTTPException): activate()
    assert calls[0][1]['binding_protocol'] == 'device-v1'
    assert path.read_text() == installed

@pytest.mark.parametrize('failure', ['legacy_response', 'audit_failure'])
def test_bound_reactivation_preserves_file_on_failure(portal, failure):
    device, routes, calls, response, activate = portal
    (upload, path, old, audit, db, lm, sign), fields = device
    installed = sign(**fields, key='TEST-ONLY').decode()
    lm.save_license_file(installed)
    if failure == 'legacy_response': response['body'] = {'license_file': old}
    else:
        response['body'] = {'license_file': sign(**fields, max_users=374).decode()}
        db.commit.side_effect = RuntimeError('test audit commit failure')
    with pytest.raises(HTTPException):
        asyncio.run(routes.reactivate_license(current_user={'id':1}, db=db, license_mutation_allowed=None))
    assert calls[0][1]['binding_protocol'] == 'device-v1'
    assert path.read_text() == installed and lm.get_license().valid
