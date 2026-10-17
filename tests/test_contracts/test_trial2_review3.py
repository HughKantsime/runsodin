"""Third review round, ODIN side (N5): any challenge failure means "no proof".

A paid key must activate exactly as before when the license server's challenge
answer is unusable (network error, error status, non-JSON 200, missing nonce);
the activation is then sent without proof of possession.
"""
import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

PROBE = r'''
import types
from modules.system import routes_health as RH
MODE = {"challenge": None}
CALLS = []
class _Resp:
    def __init__(self, status, data, ctype="application/json"):
        self.status_code = status; self._d = data; self.headers = {"content-type": ctype}
    def json(self):
        if isinstance(self._d, str): raise ValueError("Expecting value: line 1 column 1")
        return self._d
class FakeAsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, params=None):
        mode = MODE["challenge"]
        if mode == "network": raise OSError("connection reset")
        if mode == "status": return _Resp(429, {"error": "Too many challenge requests."})
        if mode == "not-json": return _Resp(200, "<html>captive portal</html>", "text/html")
        if mode == "json-garbage": return _Resp(200, "{oops")
        if mode == "no-nonce": return _Resp(200, {"ttl_seconds": 120})
        if mode == "wrong-type": return _Resp(200, {"nonce": 12345})
        return _Resp(200, {"nonce": "n" * 32})
    async def post(self, url, json=None):
        CALLS.append(json)
        return _Resp(200, {"license_file": sign_license(license_payload("pro", key=json["key"], installation_id=json["installation_id"]))})
RH.httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
seed_user("t-admin", "admin")
a = login("t-admin")
for mode in ("network", "status", "not-json", "json-garbage", "no-nonce", "wrong-type", None):
    MODE["challenge"] = mode
    n = len(CALLS)
    r = a.post("/api/license/activate", json={"key": "-".join(["ODIN", "PAID", "N5KY", secrets.token_hex(2).upper()])})
    OUT[str(mode)] = {"status": r.status_code, "proof": sorted(k for k in (CALLS[-1] if len(CALLS) > n else {}) if k in ("nonce", "bootstrap_signature"))}
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    return run_probe("trial2-review3", PROBE, tmp_path_factory.mktemp("trial2-review3"))


@pytest.mark.parametrize("mode", ["network", "status", "not-json", "json-garbage", "no-nonce", "wrong-type"])
def test_a_paid_key_activates_without_proof_when_the_challenge_is_unusable(probe, mode):
    assert probe[mode] == {"status": 200, "proof": []}


def test_with_a_usable_challenge_the_activation_carries_the_proof(probe):
    assert probe["None"] == {"status": 200, "proof": ["bootstrap_signature", "nonce"]}
