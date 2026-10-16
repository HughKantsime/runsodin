"""Cert G: F20-20.2-01/02/04/05/06 and F20-20.3-10 (ODIN half) through real HTTP.

The license portal is the external boundary: ``routes_health.httpx`` is replaced
with an in-process portal stub that issues challenges, verifies the device
proof-of-possession signatures exactly like api/_lib/license-pop.js, enforces a
1-grant slot, and returns licenses signed by the throwaway issuer key. All
sockets/DNS stay blocked for the whole probe (NET_ATTEMPTS must stay empty).
"""
import pytest

import secrets

from tests.test_contracts.test_cert_G_harness import run_probe

# Same throwaway issuer key in both processes so the restart verifies the activated file.
SEED = secrets.token_hex(32)

PORTAL_STUB = r'''
import types, base64 as _b64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from modules.system import routes_health as RH
# License keys are generated per run (no key-shaped literals in the repo).
PRO_KEY = "-".join(["ODIN", "CERT", "PROK", secrets.token_hex(2).upper()])
PORTAL = {"keys": {PRO_KEY: "pro"}, "activations": {}, "nonces": set(), "calls": [], "down": False,
          "bad_signer": None, "max_grants": 1}
class _Resp:
    def __init__(self, status, data):
        self.status_code = status; self._d = data; self.headers = {"content-type": "application/json"}
    def json(self): return self._d
def _verify_pop(action, key, inst, nonce, sig):
    act = PORTAL["activations"].get(key, {}).get(inst)
    if not act or nonce not in PORTAL["nonces"]: return False
    PORTAL["nonces"].discard(nonce)
    msg = f"odin-license-v1\n{action}\n{key}\n{inst}\n{nonce}".encode()
    try:
        Ed25519PublicKey.from_public_bytes(_b64.b64decode(act["device_pubkey"])).verify(_b64.b64decode(sig), msg); return True
    except Exception: return False
def _issue(key, inst):
    tier = PORTAL["keys"][key]
    return sign_license(license_payload(tier, key=key, installation_id=inst, features=None if False else LM.TIERS[tier]["features"],
                                        max_printers=None, max_users=None), signer=PORTAL["bad_signer"])
class FakeAsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, params=None):
        PORTAL["calls"].append(("GET", url))
        if PORTAL["down"]: raise OSError("portal unreachable")
        n = secrets.token_hex(16); PORTAL["nonces"].add(n); return _Resp(200, {"nonce": n})
    async def post(self, url, json=None):
        PORTAL["calls"].append(("POST", url))
        if PORTAL["down"]: raise OSError("portal unreachable")
        key, inst = json.get("key"), json.get("installation_id")
        if key not in PORTAL["keys"]: return _Resp(404, {"error": "License key not found"})
        acts = PORTAL["activations"].setdefault(key, {})
        if url.endswith("/api/v1/activate"):
            if inst not in acts and len(acts) >= PORTAL["max_grants"]:
                return _Resp(409, {"error": "No activation slots available"})
            acts[inst] = {"device_pubkey": json["device_pubkey"]}
            return _Resp(200, {"license_file": _issue(key, inst)})
        action = url.rsplit("/", 1)[-1]
        if not _verify_pop(action, key, inst, json.get("nonce"), json.get("signature")):
            return _Resp(403, {"error": "Proof of possession failed"})
        if action == "unactivate":
            acts.pop(inst, None); return _Resp(200, {"status": "unactivated"})
        return _Resp(200, {"license_file": _issue(key, inst)})
RH.httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
'''

FLOW = PORTAL_STUB + r'''
seed_user("cert-g-admin", "admin"); seed_user("cert-g-operator", "operator"); seed_user("cert-g-viewer", "viewer")
a = login("cert-g-admin"); op = login("cert-g-operator"); vw = login("cert-g-viewer")
inst = a.get("/api/license/installation-id").json()["installation_id"]
OUT["installation_id"] = inst
OUT["license_dir"] = LM.LICENSE_DIR
OUT["initial"] = body(a.get("/api/license"))

# ---- online activation ----
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["activate"] = {"status": r.status_code, "body": body(r)}
OUT["after_activate"] = body(client().get("/api/license"))
OUT["activate_url"] = [c for c in PORTAL["calls"] if c[0] == "POST"][-1][1]
OUT["activate_nonadmin"] = [op.post("/api/license/activate", json={"key": PRO_KEY}).status_code,
                            vw.post("/api/license/activate", json={"key": PRO_KEY}).status_code]
OUT["license_after_activate_sha"] = OUT["after_activate"]["license_sha256"]
import shutil; shutil.copytree(LM.LICENSE_DIR, LM.LICENSE_DIR + "-snapshot-after-activate")

# ---- air gap after activation: portal and network gone ----
PORTAL["down"] = True
calls_before = len(PORTAL["calls"])
LM._cached_license = None; LM._cached_mtime = 0
OUT["airgap_license"] = body(client().get("/api/license"))
OUT["airgap_printers"] = [a.post("/api/printers", json={"name": f"CertG airgap {i}", "model": "X1C", "api_type": "bambu", "slot_count": 1}).status_code for i in range(7)]
OUT["airgap_portal_calls"] = len(PORTAL["calls"]) - calls_before
OUT["activate_while_down"] = a.post("/api/license/activate", json={"key": PRO_KEY}).status_code
OUT["after_failed_activate_down"] = body(client().get("/api/license"))["tier"]
PORTAL["down"] = False

# ---- reactivation picks up a portal-side tier change ----
PORTAL["keys"][PRO_KEY] = "enterprise"
r = a.post("/api/license/reactivate")
OUT["reactivate"] = {"status": r.status_code, "body": body(r)}
OUT["after_reactivate"] = body(client().get("/api/license"))["tier"]

# ---- grant slot: a second installation cannot take the only grant ----
PORTAL["activations"][PRO_KEY]["other-install"] = {"device_pubkey": "x"}
PORTAL["activations"][PRO_KEY].pop(inst)
PORTAL["max_grants"] = 1
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["slot_full"] = {"status": r.status_code, "body": body(r)}
PORTAL["activations"][PRO_KEY] = {}
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["reactivate_after_free"] = r.status_code

# ---- unactivation frees the grant (PoP verified by portal stub) ----
r = a.post("/api/license/unactivate", json={})
OUT["unactivate"] = {"status": r.status_code, "body": body(r)}
OUT["after_unactivate"] = body(client().get("/api/license"))["tier"]
OUT["grant_freed"] = inst not in PORTAL["activations"][PRO_KEY]
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["activate_again"] = {"status": r.status_code, "tier": body(client().get("/api/license"))["tier"]}

# ---- portal returns a license not signed by the trusted issuer while a valid one is installed ----
before = body(client().get("/api/license"))
PORTAL["bad_signer"] = Ed25519PrivateKey.generate()
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["bad_sig_activate"] = {"status": r.status_code, "tier_before": before["tier"], "tier_after": body(client().get("/api/license"))["tier"]}
r = a.post("/api/license/reactivate")
after = body(client().get("/api/license"))
OUT["bad_sig_reactivate"] = {"status": r.status_code, "tier_after": after["tier"], "sha_unchanged": after["license_sha256"] == before["license_sha256"]}
PORTAL["bad_signer"] = None
'''

UPLOAD = r'''
import io, json as _j
OFFL_KEY = "-".join(["ODIN", "CERT", "OFFL", secrets.token_hex(2).upper()])
OUT["offline_key"] = OFFL_KEY
seed_user("cert-g-admin", "admin"); seed_user("cert-g-operator", "operator"); seed_user("cert-g-viewer", "viewer")
a = login("cert-g-admin"); op = login("cert-g-operator"); vw = login("cert-g-viewer")
inst = a.get("/api/license/installation-id").json()["installation_id"]
def up(c, content, name="odin.license"):
    r = c.post("/api/license/upload", files={"file": (name, content if isinstance(content, bytes) else content.encode(), "application/octet-stream")})
    return {"status": r.status_code, "body": body(r)}
good = sign_license(license_payload("pro", installation_id=inst))
OUT["operator_upload"] = up(op, good); OUT["viewer_upload"] = up(vw, good)
OUT["anon_upload"] = client().post("/api/license/upload", files={"file": ("x", good.encode())}).status_code
OUT["after_denied"] = body(client().get("/api/license"))["tier"]
OUT["admin_upload"] = up(a, good)
OUT["after_upload"] = body(client().get("/api/license"))
p, s = good.split(".")
raw = _j.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
raw["tier"] = "enterprise"
tampered = base64.urlsafe_b64encode(_j.dumps(raw).encode()).decode().rstrip("=") + "." + s
OUT["tampered_upload"] = up(a, tampered)
OUT["wrong_key_upload"] = up(a, sign_license(license_payload("enterprise", installation_id=inst), signer=Ed25519PrivateKey.generate()))
OUT["expired_upload"] = up(a, sign_license(license_payload("enterprise", installation_id=inst, expires_at="2001-01-01")))
OUT["other_install_upload"] = up(a, sign_license(license_payload("enterprise", installation_id="someone-else")))
OUT["after_rejected"] = body(client().get("/api/license"))["tier"]

# tampered file placed directly on disk (bypassing upload)
Path(LM.LICENSE_DIR, LM.LICENSE_FILENAME).write_text(tampered); LM._cached_license = None; LM._cached_mtime = 0
OUT["tampered_on_disk"] = body(client().get("/api/license"))
LM.save_license_file(good)

# ---- offline JSON exchange ----
r = a.post("/api/license/activation-request", json={"key": OFFL_KEY, "nonce": "portal-issued-nonce-1"})
OUT["activation_request"] = {"status": r.status_code, "disposition": r.headers.get("content-disposition"), "body": body(r)}
ar = r.json()["activation_request"]
msg = f"odin-license-v1\nactivate-bootstrap\n{OFFL_KEY}\n{inst}\nportal-issued-nonce-1".encode()
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
try:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(ar["device_pubkey"])).verify(base64.b64decode(ar["bootstrap_signature"]), msg)
    OUT["bootstrap_sig_valid"] = True
except Exception:
    OUT["bootstrap_sig_valid"] = False
OUT["activation_request_nonadmin"] = op.post("/api/license/activation-request", json={"key": "k", "nonce": "n"}).status_code
# Exactly the call frontend/src/api/auth.ts license.getActivationRequest(key, nonce) makes
# (pinned on the UI side by frontend/src/components/admin/LicenseTab.offline.test.tsx).
r = a.post("/api/license/activation-request", json={"key": OFFL_KEY, "nonce": "portal-issued-nonce-ui"})
OUT["activation_request_get_as_ui_calls"] = r.status_code
OUT["activation_request_blank_nonce"] = a.post("/api/license/activation-request", json={"key": OFFL_KEY, "nonce": " "}).status_code
offline = sign_license(license_payload("education", key=OFFL_KEY, installation_id=inst))
op_, sg = offline.split(".")
OUT["offline_json_import"] = up(a, _j.dumps({"payload": op_, "signature": sg}), "odin-license.json")
OUT["after_offline"] = body(client().get("/api/license"))["tier"]
OUT["offline_compact_import"] = up(a, offline)
r = a.post("/api/license/unactivation-request", json={"nonce": "portal-issued-nonce-2"})
OUT["unactivation_request"] = {"status": r.status_code, "disposition": r.headers.get("content-disposition"), "body": body(r)}
ur = r.json()
try:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(ar["device_pubkey"])).verify(
        base64.b64decode(ur["signature"]),
        f"odin-license-v1\nunactivate\n{ur['key']}\n{inst}\nportal-issued-nonce-2".encode())
    OUT["unactivation_sig_valid"] = True
except Exception:
    OUT["unactivation_sig_valid"] = False
OUT["unactivation_request_blank_nonce"] = a.post("/api/license/unactivation-request", json={"nonce": ""}).status_code
OUT["unactivation_request_nonadmin"] = op.post("/api/license/unactivation-request", json={"nonce": "n"}).status_code

# ---- portal default license with features null (cert-g-portal finding) ----
LM.save_license_file(sign_license(license_payload("pro", installation_id=inst, features=None, max_printers=None, max_users=None)))
r = client().get("/api/license")
OUT["features_null"] = {"status": r.status_code, "body": body(r)}
'''


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    return run_probe("license-flow", FLOW, tmp_path_factory.mktemp("cert-g-flow"), {"CERT_G_SIGNER_SEED": SEED})


@pytest.fixture(scope="module")
def restart(flow, tmp_path_factory):
    """Second process (restart) reusing the activated LICENSE_DIR; no portal, no network."""
    lic_dir = flow["license_dir"] + "-snapshot-after-activate"
    code = r'''
seed_user("cert-g-admin", "admin")
a = login("cert-g-admin")
OUT["license"] = body(client().get("/api/license"))
OUT["installation_id"] = a.get("/api/license/installation-id").json()["installation_id"]
OUT["printers"] = [a.post("/api/printers", json={"name": f"CertG restart {i}", "model": "X1C", "api_type": "bambu", "slot_count": 1}).status_code for i in range(7)]
'''
    return run_probe("license-restart", code, tmp_path_factory.mktemp("cert-g-restart"), {"LICENSE_DIR": lic_dir, "CERT_G_SIGNER_SEED": SEED})


@pytest.fixture(scope="module")
def upload(tmp_path_factory):
    return run_probe("license-upload", UPLOAD, tmp_path_factory.mktemp("cert-g-upload"))


@pytest.fixture(scope="module")
def custom_url(tmp_path_factory):
    code = PORTAL_STUB + r'''
seed_user("cert-g-admin", "admin"); a = login("cert-g-admin")
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["status"] = r.status_code
OUT["urls"] = [c[1] for c in PORTAL["calls"]]
'''
    return run_probe("license-url", code, tmp_path_factory.mktemp("cert-g-url"), {"LICENSE_SERVER_URL": "http://localhost:18999"})


# ---------- F20-20.2-01 Ed25519, local, no phone home ----------
def test_no_socket_or_dns_attempt_in_any_license_probe(flow, upload, restart):
    assert flow["_net_attempts"] == [] and upload["_net_attempts"] == [] and restart["_net_attempts"] == []


def test_valid_signed_license_applies_locally(upload):
    assert upload["admin_upload"]["status"] == 200 and upload["after_upload"]["tier"] == "pro"
    assert upload["after_upload"]["valid"] is True and upload["after_upload"]["binding_matches_current"] is True


@pytest.mark.parametrize("case", ["tampered_upload", "wrong_key_upload", "expired_upload", "other_install_upload"])
def test_invalid_license_rejected_and_installed_license_kept(upload, case):
    assert upload[case]["status"] == 400
    assert upload["after_rejected"] == "pro"


def test_tampered_file_on_disk_falls_back_to_community(upload):
    t = upload["tampered_on_disk"]
    assert t["valid"] is False and t["tier"] == "community" and t["max_printers"] == 5


# ---------- F20-20.2-02 air gap ----------
def test_license_keeps_working_with_portal_and_network_down(flow):
    assert flow["airgap_license"]["tier"] == "pro" and flow["airgap_license"]["valid"] is True
    assert flow["airgap_printers"] == [201] * 7
    assert flow["airgap_portal_calls"] == 0


def test_license_survives_process_restart_offline(flow, restart):
    assert restart["installation_id"] == flow["installation_id"]
    assert restart["license"]["tier"] == "pro" and restart["license"]["valid"] is True
    assert restart["printers"] == [201] * 7


def test_failed_activation_while_offline_keeps_license(flow):
    assert flow["activate_while_down"] == 502 and flow["after_failed_activate_down"] == "pro"


# ---------- F20-20.2-04 upload admin-only ----------
def test_license_upload_admin_only(upload):
    assert upload["operator_upload"]["status"] == 403 and upload["viewer_upload"]["status"] == 403
    assert upload["anon_upload"] == 401
    assert upload["after_denied"] == "community"


# ---------- F20-20.2-05 online activate / unactivate / reactivate ----------
def test_online_activation(flow):
    assert flow["initial"]["tier"] == "community"
    assert flow["activate"]["status"] == 200 and flow["activate"]["body"]["tier"] == "pro"
    assert flow["after_activate"]["tier"] == "pro" and flow["after_activate"]["binding_present"] is True
    assert flow["activate_nonadmin"] == [403, 403]


def test_reactivation_picks_up_tier_change(flow):
    assert flow["reactivate"]["status"] == 200 and flow["after_reactivate"] == "enterprise"


def test_unactivation_frees_grant_and_reverts_to_community(flow):
    assert flow["unactivate"]["status"] == 200 and flow["after_unactivate"] == "community"
    assert flow["grant_freed"] is True
    assert flow["activate_again"] == {"status": 200, "tier": "enterprise"}


def test_activation_slot_exhaustion_is_reported(flow):
    assert flow["slot_full"]["status"] == 409 and "slot" in flow["slot_full"]["body"]["detail"]
    assert flow["reactivate_after_free"] == 200


def test_bad_portal_license_does_not_destroy_installed_license(flow):
    b = flow["bad_sig_activate"]
    assert b["status"] == 400 and b["tier_before"] == "enterprise"
    assert b["tier_after"] == "enterprise"
    rb = flow["bad_sig_reactivate"]
    assert rb == {"status": 400, "tier_after": "enterprise", "sha_unchanged": True}


# ---------- F20-20.2-06 offline JSON exchange ----------
def test_offline_activation_request_file(upload):
    r = upload["activation_request"]
    assert r["status"] == 200 and "odin-activation-request.json" in r["disposition"]
    ar = r["body"]["activation_request"]
    assert r["body"]["key"] == upload["offline_key"] and ar["installation_id"]
    assert {"device_pubkey", "bootstrap_signature", "nonce", "hostname", "odin_version", "generated_at"} <= set(ar)
    assert upload["bootstrap_sig_valid"] is True
    assert upload["activation_request_nonadmin"] == 403


def test_offline_json_and_compact_import(upload):
    assert upload["offline_json_import"]["status"] == 200 and upload["after_offline"] == "education"
    assert upload["offline_compact_import"]["status"] == 200


def test_offline_unactivation_request_file(upload):
    r = upload["unactivation_request"]
    assert r["status"] == 200 and "odin-unactivation-request.json" in r["disposition"]
    assert r["body"]["key"] == upload["offline_key"] and r["body"]["installation_id"]
    # api/v1/offline.js frees a grant only with nonce + device signature (proof of possession).
    assert r["body"]["nonce"] == "portal-issued-nonce-2" and upload["unactivation_sig_valid"] is True
    assert upload["unactivation_request_blank_nonce"] == 400
    assert upload["unactivation_request_nonadmin"] == 403


def test_activation_request_as_the_ui_calls_it(upload):
    assert upload["activation_request_get_as_ui_calls"] == 200
    assert upload["activation_request_blank_nonce"] == 400


def test_portal_default_features_null_license_is_usable(upload):
    assert upload["features_null"]["status"] == 200 and upload["features_null"]["body"]["tier"] == "pro"


# ---------- F20-20.3-10 default/configurable LICENSE_SERVER_URL ----------
def test_default_license_server_url_used_by_real_route(flow):
    assert flow["activate_url"] == "https://runsodin.com/api/v1/activate"


def test_configured_license_server_url_used_by_real_route(custom_url):
    assert custom_url["status"] == 200
    assert custom_url["urls"] == ["http://localhost:18999/api/v1/activate"]


# ---------- F20-20.2-06 / 20.3-05 offline seat hand-over A -> B (cert-fix-G review HIGH 1) ----------
HANDOVER = PORTAL_STUB + r'''
import shutil
seed_user("cert-g-admin", "admin"); a = login("cert-g-admin")
dir_a = LM.LICENSE_DIR
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["a_activate"] = r.status_code
inst_a = a.get("/api/license/installation-id").json()["installation_id"]
# The admin fetches a challenge for A from the portal (portal stub mints it).
nonce = secrets.token_hex(16); PORTAL["nonces"].add(nonce)
r = a.post("/api/license/unactivation-request", json={"nonce": nonce})
OUT["request_status"] = r.status_code
req = r.json()
LM._cached_license = None; LM._cached_mtime = 0
OUT["a_after_request"] = body(client().get("/api/license"))
OUT["a_license_file_exists"] = Path(dir_a, LM.LICENSE_FILENAME).exists()
OUT["a_quarantined"] = sorted(p.name for p in Path(dir_a).glob(LM.LICENSE_FILENAME + ".unactivated-*"))
OUT["a_printers"] = [a.post("/api/printers", json={"name": f"A {i}", "model": "X1C", "api_type": "bambu", "slot_count": 1}).status_code for i in range(6)]
# A second request on A (no license any more) cannot mint another hand-over.
OUT["second_request"] = a.post("/api/license/unactivation-request", json={"nonce": "n2"}).status_code
# Portal (api/v1/offline.js semantics): frees the grant only for a valid PoP over key/installation/nonce.
OUT["portal_freed"] = _verify_pop("unactivate", req["key"], req["installation_id"], req["nonce"], req["signature"])
if OUT["portal_freed"]:
    PORTAL["activations"][req["key"]].pop(req["installation_id"], None)
OUT["replay_refused"] = not _verify_pop("unactivate", req["key"], req["installation_id"], req["nonce"], req["signature"])
# Installation B (its own LICENSE_DIR => own installation id + device key) takes the seat.
dir_b = str(WORK / "license-b"); os.makedirs(dir_b, exist_ok=True)
LM.LICENSE_DIR = dir_b; LM._cached_license = None; LM._cached_mtime = 0
r = a.post("/api/license/activate", json={"key": PRO_KEY})
OUT["b_activate"] = r.status_code
OUT["b_tier"] = body(client().get("/api/license"))["tier"]
OUT["b_installation_differs"] = a.get("/api/license/installation-id").json()["installation_id"] != inst_a
# A cannot silently come back: its seat is gone, re-activation is refused while B holds it.
LM.LICENSE_DIR = dir_a; LM._cached_license = None; LM._cached_mtime = 0
OUT["a_reactivate_while_b_holds"] = a.post("/api/license/activate", json={"key": PRO_KEY}).status_code
OUT["a_tier_end"] = body(client().get("/api/license"))["tier"]
'''


@pytest.fixture(scope="module")
def handover(tmp_path_factory):
    return run_probe("license-handover", HANDOVER, tmp_path_factory.mktemp("cert-g-handover"))


def test_offline_unactivation_request_removes_the_local_license(handover):
    assert handover["a_activate"] == 200 and handover["request_status"] == 200
    a = handover["a_after_request"]
    assert a["tier"] == "community" and a["valid"] is False and a["max_printers"] == 5
    assert handover["a_license_file_exists"] is False and len(handover["a_quarantined"]) == 1
    assert handover["a_printers"] == [201] * 5 + [403]
    assert handover["second_request"] == 400


def test_offline_seat_moves_from_a_to_b_exactly_once(handover):
    assert handover["portal_freed"] is True and handover["replay_refused"] is True
    assert handover["b_activate"] == 200 and handover["b_tier"] == "pro" and handover["b_installation_differs"] is True
    assert handover["a_reactivate_while_b_holds"] == 409 and handover["a_tier_end"] == "community"
    assert handover["_net_attempts"] == []
