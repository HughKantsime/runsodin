"""Second review round of the trial redesign, ODIN side (L1, L4, M1 display).

* L1: a trial license names its device key; ODIN refuses one issued to another
  device key, and online activation proves possession of the device key.
* L4: the portal's 401/403 and unreadable answers become 502 (the admin's
  session is never treated as expired), with the portal's error code.
* M1: the portal's ADDRESS_LIMIT 429 reaches the admin with its message.
"""
import base64
import json

import pytest

from tests.test_contracts.test_cert_G_harness import run_probe


@pytest.fixture
def issuer(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import license_manager as lm
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(lm, "ODIN_PUBLIC_KEY", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    monkeypatch.setattr(lm, "LICENSE_DIR", str(tmp_path))
    for name in ("ODIN_LICENSE_FILE", "ODIN_REQUIRE_LICENSE_BINDING", "ODIN_LICENSE_READ_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lm, "_cached_license", None)
    monkeypatch.setattr(lm, "_cached_mtime", 0)
    return key


def _license(issuer, **payload):
    raw = json.dumps({"tier": "pro", "licensee": "Pro trial", "expires_at": "2099-01-01T00:00:00+00:00", **payload}).encode()
    return base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(issuer.sign(raw)).decode()


def _other_pub():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def test_a_trial_license_for_this_device_key_is_valid(issuer):
    import license_manager as lm
    lm.save_license_file(_license(issuer, trial=True, device_pubkey=lm.get_device_keypair()[1]))
    info = lm.get_license()
    assert info.valid and info.trial and info.error is None


def test_a_trial_license_issued_to_another_device_key_is_refused(issuer):
    import license_manager as lm
    lm.get_device_keypair()
    lm.save_license_file(_license(issuer, trial=True, device_pubkey=_other_pub()))
    info = lm.get_license()
    assert info.valid is False
    assert info.error == "This trial license was issued to a different device key. Start or activate the trial from this server."
    assert not info.has_feature("orders")


def test_the_install_path_refuses_a_trial_license_for_another_device_key(issuer):
    from fastapi import HTTPException
    import license_manager as lm
    lm.get_device_keypair()
    with pytest.raises(HTTPException) as exc:
        lm.install_portal_license(_license(issuer, trial=True, device_pubkey=_other_pub()))
    assert exc.value.status_code == 400 and "different device key" in exc.value.detail
    assert lm._find_license_file() is None


def test_paid_licenses_are_not_bound_to_the_device_key_in_2_0(issuer):
    import license_manager as lm
    lm.get_device_keypair()
    lm.save_license_file(_license(issuer, device_pubkey=_other_pub()))  # ignored for paid keys
    assert lm.get_license().valid


PROBE = r'''
import types
from modules.system import routes_health as RH
PORTAL = {"calls": [], "answer": (202, {"status": "request_received"}, "application/json")}
class _Resp:
    def __init__(self, status, data, ctype):
        self.status_code = status; self._d = data; self.headers = {"content-type": ctype}
    def json(self):
        if isinstance(self._d, str): raise ValueError("not json")
        return self._d
class FakeAsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, params=None):
        PORTAL["calls"].append(("GET", url, params)); return _Resp(200, {"nonce": "n" * 32}, "application/json")
    async def post(self, url, json=None):
        PORTAL["calls"].append(("POST", url, json))
        if url.endswith("/api/v1/activate"):
            key = json["key"]
            return _Resp(200, {"license_file": sign_license(license_payload("pro", key=key, installation_id=json["installation_id"]))}, "application/json")
        s, b, c = PORTAL["answer"]; return _Resp(s, b, c)
RH.httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
seed_user("t-admin", "admin")
a = login("t-admin")
def trial():
    r = a.post("/api/license/trial-request", json={"email": "owner@example.com"})
    return {"status": r.status_code, "body": body(r)}
PORTAL["answer"] = (401, {"error": "The trial request signature is not valid.", "code": "BAD_SIGNATURE"}, "application/json")
OUT["sig401"] = trial()
OUT["still_logged_in"] = a.get("/api/auth/me").status_code
PORTAL["answer"] = (403, {"error": "Forbidden", "code": "FORBIDDEN"}, "application/json")
OUT["forbidden403"] = trial()
PORTAL["answer"] = (502, "<html>bad gateway</html>", "application/json")
OUT["unreadable"] = trial()
PORTAL["answer"] = (200, {"status": "weird"}, "application/json")
OUT["unexpected"] = trial()
PORTAL["answer"] = (429, {"error": "This address has received the maximum trial emails today; try again tomorrow or use another address.", "code": "ADDRESS_LIMIT"}, "application/json")
OUT["address_limit"] = trial()
# Online activation carries proof of possession of the device key.
key = "-".join(["ODIN", "TRIA", "LPOP", secrets.token_hex(2).upper()])
r = a.post("/api/license/activate", json={"key": key})
OUT["activate"] = r.status_code
OUT["activate_call"] = [c for c in PORTAL["calls"] if c[0] == "POST" and c[1].endswith("/api/v1/activate")][-1][2]
OUT["challenge_call"] = [c for c in PORTAL["calls"] if c[0] == "GET"][-1]
OUT["installation_id"] = LM.get_installation_id()
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    return run_probe("trial2-review2", PROBE, tmp_path_factory.mktemp("trial2-review2"))


def test_portal_401_and_403_become_502_with_the_portal_code_and_never_log_out(probe):
    for name, code in (("sig401", "BAD_SIGNATURE"), ("forbidden403", "FORBIDDEN")):
        r = probe[name]
        assert r["status"] == 502, r
        assert r["body"]["detail"]["portal_code"] == code
        assert r["body"]["detail"]["portal_status"] in (401, 403)
    assert probe["still_logged_in"] == 200


def test_unreadable_or_unexpected_portal_answers_are_502(probe):
    assert probe["unreadable"]["status"] == 502
    assert probe["unexpected"]["status"] == 502


def test_the_address_limit_reaches_the_admin(probe):
    r = probe["address_limit"]
    assert r["status"] == 429
    assert r["body"]["detail"]["portal_code"] == "ADDRESS_LIMIT"
    assert "try again tomorrow or use another address" in r["body"]["detail"]["message"]


def test_online_activation_proves_possession_of_the_device_key(probe):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    call = probe["activate_call"]
    assert probe["challenge_call"][1].endswith("/api/v1/challenge")
    assert call["nonce"] == "n" * 32 and call["installation_id"] == probe["installation_id"]
    msg = f"odin-license-v1\nactivate-bootstrap\n{call['key']}\n{call['installation_id']}\n{call['nonce']}".encode()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(call["device_pubkey"])).verify(base64.b64decode(call["bootstrap_signature"]), msg)
