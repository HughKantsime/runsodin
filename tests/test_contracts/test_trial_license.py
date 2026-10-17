"""14-day Pro trial, ODIN side (spec .claude/specs/pro-trial.md).

Signed trial licenses (throwaway issuer key, the same seam as every cert-G
license test) go through the real verifier, the real app factory, real login,
RBAC and the real Pro gates. The license portal is stubbed only at the httpx
boundary for the online activation probe; the real portal is exercised end to
end in odin-site tests/cert-trial-portal-pg.test.js through odin_bridge.py.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

PRO_FEATURES = {
    "multi_user", "rbac", "sso", "white_label", "permissions", "branding", "webhooks",
    "email_notifications", "push_notifications", "orders", "products", "bom", "analytics",
    "csv_export", "cost_calculator", "maintenance", "care_counters", "usage_reports",
}


def _in(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


# ── Verifier: trial flag, days left, signature coverage ─────────────────────

def test_activating_a_trial_license_gives_every_pro_feature(tier_license):
    import license_manager as lm
    info = tier_license("pro", trial=True, expires_at=_in(days=14), max_printers=None, max_users=None)
    assert info.valid and info.trial is True and info.tier == "pro"
    assert PRO_FEATURES <= set(info.effective_features())
    assert all(info.has_feature(f) for f in PRO_FEATURES)
    assert info.max_printers is None and info.max_users is None
    public = info.to_public_dict()
    assert public["trial"] is True and public["trial_days_left"] == 14
    assert lm.get_license().to_dict()["trial_days_left"] == 14


@pytest.mark.parametrize("remaining, shown", [
    (timedelta(days=14), 14), (timedelta(days=13, hours=12), 14), (timedelta(days=3), 3),
    (timedelta(days=2, hours=1), 3), (timedelta(hours=20), 1), (timedelta(seconds=30), 1),
])
def test_trial_days_left_rounds_up_to_whole_days(tier_license, remaining, shown):
    import license_manager as lm
    tier_license("pro", trial=True, expires_at=(datetime.now(timezone.utc) + remaining).isoformat())
    assert lm.get_license().trial_days_left() == shown


def test_a_paid_license_is_not_a_trial(tier_license):
    info = tier_license("pro", expires_at=_in(days=365))
    assert info.trial is False and info.trial_days_left() is None
    assert info.to_public_dict()["trial"] is False and info.to_public_dict()["trial_days_left"] is None


@pytest.mark.parametrize("value", ["true", 1, "yes", None, False])
def test_only_a_signed_boolean_true_marks_a_trial(tier_license, value):
    info = tier_license("pro", trial=value, expires_at=_in(days=14))
    assert info.trial is False


def test_an_ended_trial_reports_trial_ended_on_community_limits(issuer):
    import license_manager as lm
    _, text = _sign(issuer, {"tier": "pro", "licensee": "Pro trial", "trial": True,
                             "expires_at": "2001-01-01T00:00:00+00:00"})
    lm.save_license_file(text)
    info = lm.get_license()
    assert info.valid is False and info.expired is True and info.trial is True
    assert info.trial_days_left() == 0
    assert info.max_users == 1 and info.max_printers == 5 and not info.has_feature("orders")
    public = info.to_public_dict()
    assert (public["trial"], public["expired"], public["trial_days_left"]) == (True, True, 0)


@pytest.fixture
def issuer(tmp_path, monkeypatch):
    """Throwaway trusted issuer key and a per-test LICENSE_DIR (the tier_license seam)."""
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


def _sign(issuer, payload):
    raw = json.dumps(payload).encode()
    return raw, base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(issuer.sign(raw)).decode()


@pytest.mark.parametrize("tamper", [
    lambda p: p.pop("trial"),                                  # drop the trial marker
    lambda p: p.update(expires_at="2099-01-01T00:00:00+00:00"),  # extend the trial
    lambda p: p.update(tier="enterprise"),                     # raise the tier
])
def test_editing_a_signed_trial_breaks_the_signature(issuer, tamper):
    import license_manager as lm
    payload = {"tier": "pro", "licensee": "Pro trial", "trial": True, "expires_at": _in(days=14)}
    _, good = _sign(issuer, payload)
    assert lm.verify_license_text(good).valid
    tampered = dict(payload)
    tamper(tampered)
    forged = base64.urlsafe_b64encode(json.dumps(tampered).encode()).decode() + "." + good.split(".")[1]
    info = lm.verify_license_text(forged)
    assert info.valid is False and info.error == "Invalid license signature" and info.trial is False


# ── Real app: Pro writes, days-left wire, expiry, notice, paid replacement ──

ACTIVE = r'''
admin_id = seed_user("trial-admin", "admin")
from datetime import datetime, timedelta, timezone
install_license("pro", trial=True, licensee="Pro trial", max_printers=None, max_users=None,
                expires_at=(datetime.now(timezone.utc) + timedelta(days=14)).isoformat())
a = login("trial-admin")
OUT["license"] = body(client().get("/api/license"))
OUT["orders"] = a.post("/api/orders", json={"order_number": "TRIAL-1", "customer_name": "Trial"}).status_code
OUT["products"] = a.post("/api/products", json={"name": "Trial product"}).status_code
OUT["maintenance"] = a.post("/api/maintenance/tasks", json={"name": "Trial task", "interval_print_hours": 10}).status_code
OUT["analytics"] = a.get("/api/analytics").status_code
OUT["branding"] = a.put("/api/branding", json={"app_name": "Trial Farm"}).status_code
OUT["users"] = [a.post("/api/users", json={"username": f"trial-user-{i}", "email": f"trial-user-{i}@example.test",
                "password": "Trial-User-Password-1!", "role": "viewer"}).status_code for i in range(3)]
OUT["printers"] = [a.post("/api/printers", json={"name": f"Trial printer {i}", "model": "X1C", "api_type": "bambu",
                   "slot_count": 1}).status_code for i in range(7)]
OUT["notices"] = body(a.get("/api/system/notices"))
'''


@pytest.fixture(scope="module")
def active(tmp_path_factory):
    return run_probe("trial-active", ACTIVE, tmp_path_factory.mktemp("trial-active"))


def test_trial_license_unlocks_pro_writes_through_the_real_app(active):
    lic = active["license"]
    assert (lic["valid"], lic["tier"], lic["trial"], lic["trial_days_left"]) == (True, "pro", True, 14)
    assert PRO_FEATURES <= set(lic["features"])
    assert {active["orders"], active["products"], active["maintenance"]} <= {200, 201}
    assert active["analytics"] == 200 and active["branding"] == 200
    assert all(s in (200, 201) for s in active["users"])      # beyond Community's 1 user
    assert all(s in (200, 201) for s in active["printers"])   # beyond Community's 5 printers
    assert active["_net_attempts"] == []


def test_no_trial_notice_while_more_than_three_days_are_left(active):
    assert [n for n in active["notices"]["notices"] if n["kind"] == "license_trial_ending"] == []


NOTICE = r'''
from datetime import datetime, timedelta, timezone
seed_user("trial-admin", "admin"); seed_user("trial-admin-2", "admin")
seed_user("trial-operator", "operator")
install_license("pro", trial=True, expires_at=(datetime.now(timezone.utc) + timedelta(days=2, hours=12)).isoformat())
a = login("trial-admin"); a2 = login("trial-admin-2"); op = login("trial-operator")
def kinds(c):
    r = c.get("/api/system/notices")
    return r.status_code, [n for n in body(r).get("notices", [])] if r.status_code == 200 else body(r)
OUT["first"] = kinds(a)
OUT["second_fetch"] = kinds(a)
OUT["operator"] = kinds(op)
OUT["operator_dismiss"] = op.post("/api/system/notices/license_trial_ending/dismiss").status_code
OUT["unknown_kind"] = a.post("/api/system/notices/restore_quarantined/dismiss").status_code
OUT["dismiss"] = {"status": (r := a.post("/api/system/notices/license_trial_ending/dismiss")).status_code, "body": body(r)}
OUT["after_dismiss"] = kinds(a)
OUT["after_dismiss_relogin"] = kinds(login("trial-admin"))
OUT["other_admin"] = kinds(a2)
OUT["dismiss_again"] = a.post("/api/system/notices/license_trial_ending/dismiss").status_code
OUT["stored"] = sql("SELECT key, value FROM system_config WHERE key LIKE 'notice_dismissed:%'")
# A paid key: no trial notice at all, and nothing to dismiss.
install_license("pro", expires_at="2099-01-01")
OUT["paid"] = kinds(a2)
OUT["paid_dismiss"] = a2.post("/api/system/notices/license_trial_ending/dismiss").status_code
'''


@pytest.fixture(scope="module")
def notice(tmp_path_factory):
    return run_probe("trial-notice", NOTICE, tmp_path_factory.mktemp("trial-notice"))


def test_admins_get_the_trial_ending_notice_three_days_out(notice):
    status, notices = notice["first"]
    assert status == 200
    trial = [n for n in notices if n["kind"] == "license_trial_ending"]
    assert len(trial) == 1
    n = trial[0]
    assert n["title"] == "Pro trial ends in 3 days" and n["days_left"] == 3 and n["severity"] == "warning"
    assert n["link"] == "/settings?tab=license" and n["buy_url"] == "https://runsodin.com/pricing"
    assert n["dismissible"] is True and "data is kept" in n["message"]
    assert notice["second_fetch"] == notice["first"]  # stays until the admin dismisses it


def test_the_trial_notice_is_shown_once_per_admin(notice):
    assert notice["dismiss"]["status"] == 200 and notice["dismiss"]["body"]["status"] == "dismissed"
    assert notice["after_dismiss"] == [200, []]
    assert notice["after_dismiss_relogin"] == [200, []]          # persisted server-side, not per session
    assert [n["kind"] for n in notice["other_admin"][1]] == ["license_trial_ending"]  # other admins still see it
    assert notice["dismiss_again"] == 200
    assert len(notice["stored"]) == 1 and notice["stored"][0][0].startswith("notice_dismissed:license_trial_ending:")


def test_trial_notice_is_admin_only_and_only_for_trials(notice):
    assert notice["operator"][0] == 403 and notice["operator_dismiss"] == 403
    assert notice["unknown_kind"] == 404
    assert notice["paid"] == [200, []] and notice["paid_dismiss"] == 404


EXPIRY = r'''
import time
from datetime import datetime, timedelta, timezone
seed_user("trial-admin", "admin")
deadline = datetime.now(timezone.utc) + timedelta(seconds=6)
install_license("pro", trial=True, expires_at=deadline.isoformat())
a = login("trial-admin")
o = a.post("/api/orders", json={"order_number": "TRIAL-EXP-1", "customer_name": "Trial"}).json()["id"]
o2 = a.post("/api/orders", json={"order_number": "TRIAL-EXP-2", "customer_name": "Trial"}).json()["id"]
p = a.post("/api/products", json={"name": "Trial expiry product"}).json()["id"]
m = a.post("/api/maintenance/tasks", json={"name": "Trial expiry task", "interval_print_hours": 10}).json()["id"]
OUT["before"] = body(client().get("/api/license"))
time.sleep(max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds()) + 0.5)
R = OUT["after"] = {}
R["license"] = body(client().get("/api/license"))
R["order_create"] = a.post("/api/orders", json={"order_number": "TRIAL-EXP-3", "customer_name": "Trial"}).status_code
R["order_patch"] = a.patch(f"/api/orders/{o}", json={"notes": "x"}).status_code
R["product_create"] = a.post("/api/products", json={"name": "After"}).status_code
R["product_patch"] = a.patch(f"/api/products/{p}", json={"name": "renamed"}).status_code
R["maint_create"] = a.post("/api/maintenance/tasks", json={"name": "After", "interval_print_hours": 1}).status_code
R["maint_patch"] = a.patch(f"/api/maintenance/tasks/{m}", json={"name": "renamed"}).status_code
R["branding_put"] = a.put("/api/branding", json={"app_name": "After"}).status_code
R["detail_403"] = body(a.post("/api/orders", json={"order_number": "TRIAL-EXP-4", "customer_name": "Trial"}))
R["order_get"] = a.get(f"/api/orders/{o}").status_code
R["orders_list"] = sorted(x["id"] for x in a.get("/api/orders").json())
R["product_get"] = a.get(f"/api/products/{p}").status_code
R["maint_list"] = [x["id"] for x in a.get("/api/maintenance/tasks").json()]
R["invoice"] = a.get(f"/api/orders/{o}/invoice.pdf").status_code
ex = a.get("/api/export/jobs")
R["export_jobs"] = (ex.status_code, ex.headers.get("content-type", ""))
R["gdpr_export"] = a.get(f"/api/users/{sql('SELECT id FROM users WHERE username=:u', u='trial-admin')[0][0]}/export").status_code
R["order_delete"] = a.delete(f"/api/orders/{o2}").status_code
R["notices"] = body(a.get("/api/system/notices"))
R["counts"] = {"orders": sql("SELECT COUNT(*) FROM orders")[0][0], "products": sql("SELECT COUNT(*) FROM products")[0][0],
               "tasks": sql("SELECT COUNT(*) FROM maintenance_tasks")[0][0]}
'''


@pytest.fixture(scope="module")
def expired(tmp_path_factory):
    return run_probe("trial-expiry", EXPIRY, tmp_path_factory.mktemp("trial-expiry"))


def test_at_expiry_pro_writes_return_403(expired):
    assert expired["before"]["valid"] is True and expired["before"]["trial"] is True
    r = expired["after"]
    assert (r["order_create"], r["order_patch"], r["product_create"], r["product_patch"],
            r["maint_create"], r["maint_patch"], r["branding_put"]) == (403,) * 7
    assert "requires Pro" in r["detail_403"]["detail"]


def test_at_expiry_reads_exports_and_deletes_still_work_and_no_data_is_deleted(expired):
    r = expired["after"]
    assert r["order_get"] == 200 and len(r["orders_list"]) == 2
    assert r["product_get"] == 200 and len(r["maint_list"]) == 1
    assert r["invoice"] == 200 and r["export_jobs"][0] == 200 and "csv" in r["export_jobs"][1]
    assert r["gdpr_export"] == 200
    assert r["order_delete"] == 204
    assert r["counts"] == {"orders": 1, "products": 1, "tasks": 1}


def test_at_expiry_the_license_reports_trial_ended_without_a_restart(expired):
    lic = expired["after"]["license"]
    assert (lic["valid"], lic["expired"], lic["trial"], lic["trial_days_left"], lic["tier"]) == (False, True, True, 0, "pro")
    assert lic["max_users"] == 1 and "orders" not in lic["features"]
    assert [n for n in expired["after"]["notices"]["notices"] if n["kind"] == "license_trial_ending"] == []


UPGRADE = r'''
import types
from datetime import datetime, timedelta, timezone
from modules.system import routes_health as RH
KEYS = {"-".join(["ODIN", "TRIA", "LKEY", secrets.token_hex(2).upper()]): "trial",
        "-".join(["ODIN", "PAID", "PKEY", secrets.token_hex(2).upper()]): "paid"}
TRIAL_KEY, PAID_KEY = list(KEYS)
TRIAL_EXPIRES = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
class _Resp:
    def __init__(self, status, data): self.status_code = status; self._d = data; self.headers = {"content-type": "application/json"}
    def json(self): return self._d
class FakeAsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, params=None): return _Resp(200, {"nonce": secrets.token_hex(16)})
    async def post(self, url, json=None):
        kind = KEYS.get(json.get("key"))
        if kind is None: return _Resp(404, {"error": "Invalid license key"})
        over = {"trial": True, "expires_at": TRIAL_EXPIRES} if kind == "trial" else {"expires_at": "2099-01-01T00:00:00+00:00"}
        return _Resp(200, {"license_file": sign_license(license_payload("pro", key=json["key"],
                     installation_id=json["installation_id"], licensee="Pro trial" if kind == "trial" else "Paid Farm", **over)),
                     "status": "activated"})
RH.httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
seed_user("trial-admin", "admin")
a = login("trial-admin")
OUT["start"] = body(client().get("/api/license"))
r = a.post("/api/license/activate", json={"key": TRIAL_KEY})
OUT["trial_activate"] = r.status_code
OUT["trial"] = body(client().get("/api/license"))
r = a.post("/api/license/activate", json={"key": PAID_KEY})
OUT["paid_activate"] = r.status_code
OUT["paid"] = body(client().get("/api/license"))
OUT["paid_admin"] = body(a.get("/api/license/installation-id"))
OUT["license_files"] = sorted(p.name for p in Path(LM.LICENSE_DIR).iterdir() if p.name.startswith("odin.license"))
# The offline/file path replaces a trial the same way.
install_license("pro", trial=True, expires_at=TRIAL_EXPIRES)
import io
paid_file = sign_license(license_payload("pro", licensee="Paid Upload", expires_at="2099-01-01T00:00:00+00:00"))
r = a.post("/api/license/upload", files={"file": ("odin.license", io.BytesIO(paid_file.encode()), "application/octet-stream")})
OUT["upload"] = r.status_code
OUT["after_upload"] = body(client().get("/api/license"))
'''


@pytest.fixture(scope="module")
def upgrade(tmp_path_factory):
    return run_probe("trial-upgrade", UPGRADE, tmp_path_factory.mktemp("trial-upgrade"))


def test_activating_a_paid_key_replaces_the_trial(upgrade):
    assert upgrade["start"]["tier"] == "community" and upgrade["start"]["trial"] is False
    assert upgrade["trial_activate"] == 200
    t = upgrade["trial"]
    assert (t["valid"], t["tier"], t["trial"], t["trial_days_left"]) == (True, "pro", True, 14)
    assert upgrade["paid_activate"] == 200
    p = upgrade["paid"]
    assert (p["valid"], p["tier"], p["trial"], p["trial_days_left"]) == (True, "pro", False, None)
    assert p["expires_at"] == "2099-01-01T00:00:00+00:00" and p["license_sha256"] != t["license_sha256"]
    assert upgrade["license_files"] == ["odin.license"]  # replaced in place, no trial copy left behind
    assert upgrade["_net_attempts"] == []


def test_uploading_a_paid_license_file_replaces_the_trial(upgrade):
    assert upgrade["upload"] == 200
    after = upgrade["after_upload"]
    assert (after["valid"], after["trial"], after["trial_days_left"]) == (True, False, None)


# ── Producer wire fixture for the frontend tests ────────────────────────────

WIRE_FIXTURE = "frontend/src/test-fixtures/trial-license-wire.json"
VOLATILE = ("license_sha256", "expires_at", "issued_at")


def test_trial_wire_capture(active, notice, expired):
    """Captured /api/license (active and ended trial) and /system/notices bodies the UI tests consume."""
    from pathlib import Path
    trial_notice = [n for n in notice["first"][1] if n["kind"] == "license_trial_ending"][0]
    captured = {
        "active_license": active["license"],
        "ended_license": expired["after"]["license"],
        "trial_ending_notice": trial_notice,
    }
    strip = lambda d: {k: v for k, v in d.items() if k not in VOLATILE}
    path = Path(__file__).resolve().parents[2] / WIRE_FIXTURE
    if path.exists():
        saved = json.loads(path.read_text())
        for name, body in captured.items():
            assert strip(saved[name]) == strip(body), name
    else:
        path.write_text(json.dumps({
            "scope": "Actual HTTP bodies from tests/test_contracts/test_trial_license.py probes (real app, signed trial licenses)",
            "volatile_fields": list(VOLATILE), **captured}, indent=2) + "\n")
