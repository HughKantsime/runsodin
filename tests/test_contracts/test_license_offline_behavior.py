"""Actual local signed-license verification with network construction forbidden."""
import base64
import json
import socket
import pytest
from tests.test_contracts.test_license_user_limits import signed_license


@pytest.fixture
def network_denied(monkeypatch):
    attempts=[]
    def deny_network(*args,**kwargs):
        attempts.append(True)
        raise AssertionError('License verification attempted network access')
    monkeypatch.setattr(socket,'socket',deny_network)
    monkeypatch.setattr(socket,'create_connection',deny_network)
    return attempts


def test_signed_license_loads_repeatedly_with_network_unavailable(signed_license,network_denied):
    manager,initial=signed_license(max_users=374)
    assert initial.valid and initial.tier=='education'
    for _ in range(3):
        loaded=manager.load_license()
        assert loaded.valid and loaded.max_users==374
        assert loaded.features==initial.features
    assert network_denied==[]


def test_altered_signed_payload_cannot_grant_entitlements_offline(signed_license,tmp_path,network_denied):
    manager,initial=signed_license(max_users=2)
    assert initial.valid
    path=tmp_path/'odin.license'
    body,signature=path.read_text().split('.')
    payload=json.loads(base64.urlsafe_b64decode(body))
    payload['max_users']=1000
    path.write_text(base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()+'.'+signature)
    loaded=manager.load_license()
    assert not loaded.valid
    assert loaded.tier=='community'
    assert loaded.max_users==1
    assert network_denied==[]


def test_wrong_verifier_key_refuses_license(signed_license,monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    manager,initial=signed_license(max_users=374)
    assert initial.valid
    wrong_key=Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(manager,'ODIN_PUBLIC_KEY',wrong_key)
    assert not manager.load_license().valid
