"""Final-review fixes on the ODIN side (spec .claude/specs/review-fixes-2.0.md).

* Trial redesign: the trial starts inside ODIN with a request signed by the
  installation's device key (the key activation binds), online or as an
  offline request code. The portal is stubbed at the httpx boundary only.
  (O13/O14 are in test_license_tiers_unlimited_and_defaults.py.)
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_contracts.test_cert_G_harness import run_probe


def _canonical(r):
    return "\n".join(["odin-trial-request-v1", r["installation_id"], r["device_pubkey"], r["email"], r["nonce"], r["issued_at"]])


def _verify(r):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(r["device_pubkey"])).verify(
        base64.b64decode(r["signature"]), _canonical(r).encode("utf-8"))


@pytest.fixture
def device(tmp_path, monkeypatch):
    import license_manager as lm
    monkeypatch.setattr(lm, "LICENSE_DIR", str(tmp_path))
    return lm


# ── Trial request signed with the device key ─────────────────────────────────

def test_trial_request_is_signed_by_the_activation_device_key(device):
    lm = device
    r = lm.build_trial_request("  Farm.Owner+odin@Example.com ")
    assert r["v"] == 1
    assert r["installation_id"] == lm.get_installation_id()
    assert r["device_pubkey"] == lm.get_device_keypair()[1]  # the same key activation binds
    assert r["email"] == "Farm.Owner+odin@Example.com"
    assert len(base64.urlsafe_b64decode(r["nonce"] + "==")) >= 16
    issued = datetime.fromisoformat(r["issued_at"].replace("Z", "+00:00"))
    assert abs(datetime.now(timezone.utc) - issued) < timedelta(seconds=5) and r["issued_at"].endswith("Z")
    _verify(r)  # raises on a bad signature
    tampered = dict(r, email="someone.else@example.com")
    with pytest.raises(Exception):
        _verify(tampered)
    assert lm.build_trial_request("a@example.com")["nonce"] != lm.build_trial_request("a@example.com")["nonce"]


def test_offline_request_code_round_trips(device):
    lm = device
    r = lm.build_trial_request("school@example.org")
    code = lm.trial_request_code(r)
    assert code.startswith("ODINTRIAL1.")
    assert json.loads(base64.urlsafe_b64decode(code.split(".", 1)[1] + "==")) == r


@pytest.mark.parametrize("bad", ["Me <me@example.com>", "me@example.com,", "me@example.com.", "a;b@example.com", "", "x" * 300 + "@e.com"])
def test_trial_request_refuses_anything_but_one_address(device, bad):
    with pytest.raises(ValueError):
        device.build_trial_request(bad)


TRIAL_ROUTE = r'''
import types
from modules.system import routes_health as RH
PORTAL = {"calls": [], "status": 202}
class _Resp:
    def __init__(self, status, data): self.status_code = status; self._d = data; self.headers = {"content-type": "application/json"}
    def json(self): return self._d
class FakeAsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None):
        PORTAL["calls"].append((url, json))
        return _Resp(PORTAL["status"], {"status": "request_received"} if PORTAL["status"] == 202 else {"error": "Trial service temporarily unavailable."})
RH.httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
seed_user("t-admin", "admin"); seed_user("t-op", "operator")
a = login("t-admin"); op = login("t-op")
r = a.post("/api/license/trial-request", json={"email": "owner@example.com"})
OUT["online"] = {"status": r.status_code, "body": body(r)}
OUT["portal_calls"] = PORTAL["calls"][:]
OUT["device_pubkey"] = LM.get_device_keypair()[1]
OUT["installation_id"] = LM.get_installation_id()
n = len(PORTAL["calls"])
r = a.post("/api/license/trial-request", json={"email": "owner@example.com", "offline": True})
OUT["offline"] = {"status": r.status_code, "body": body(r)}
OUT["offline_portal_calls"] = len(PORTAL["calls"]) - n
OUT["operator"] = op.post("/api/license/trial-request", json={"email": "owner@example.com"}).status_code
OUT["bad_email"] = a.post("/api/license/trial-request", json={"email": "Me <me@example.com>"}).status_code
PORTAL["status"] = 503
r = a.post("/api/license/trial-request", json={"email": "owner@example.com"})
OUT["portal_down"] = {"status": r.status_code, "body": body(r)}
install_license("pro", expires_at="2099-01-01")
OUT["already_licensed"] = a.post("/api/license/trial-request", json={"email": "owner@example.com", "offline": True}).status_code
'''


@pytest.fixture(scope="module")
def trial_route(tmp_path_factory):
    return run_probe("trial2-route", TRIAL_ROUTE, tmp_path_factory.mktemp("trial2-route"))


def test_settings_start_trial_posts_the_signed_request_to_the_portal(trial_route):
    assert trial_route["online"] == {"status": 200, "body": {"status": "request_sent"}}
    (url, payload), = trial_route["portal_calls"]
    assert url.endswith("/api/v1/trial")
    r = payload["request"]
    assert r["installation_id"] == trial_route["installation_id"] and r["device_pubkey"] == trial_route["device_pubkey"]
    assert r["email"] == "owner@example.com"
    _verify(r)
    assert trial_route["_net_attempts"] == []


def test_offline_start_returns_a_request_code_without_any_network(trial_route):
    off = trial_route["offline"]
    assert off["status"] == 200 and off["body"]["request_code"].startswith("ODINTRIAL1.")
    assert off["body"]["expires_in_minutes"] == 15
    _verify(json.loads(base64.urlsafe_b64decode(off["body"]["request_code"].split(".", 1)[1] + "==")))
    assert trial_route["offline_portal_calls"] == 0


def test_trial_request_is_superadmin_only_and_validates(trial_route):
    assert trial_route["operator"] == 403
    assert trial_route["bad_email"] == 422 or trial_route["bad_email"] == 400
    assert trial_route["portal_down"]["status"] == 503
    assert trial_route["already_licensed"] == 409
