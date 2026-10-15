"""Cert G: F20-20.1-01..04 tier enforcement through the real app factory.

Each tier gets a fresh bootstrapped SQLite DB and a signed (throwaway-key)
license installed in LICENSE_DIR (Community = no license file, i.e. free).
All calls are real HTTP through create_app() with a real password-login JWT.
"""
import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

PROBE = r'''
tier = os.environ["CERT_G_TIER"]
admin_id = seed_user("cert-g-admin", "admin")
install_license(None if tier == "community" else tier)
a = login("cert-g-admin")
OUT["license"] = body(a.get("/api/license"))

OUT["printer_create"] = []
for i in range(12):
    r = a.post("/api/printers", json={"name": f"CertG tier printer {i}", "model": "X1C", "api_type": "bambu", "slot_count": 1})
    OUT["printer_create"].append(r.status_code)
    if r.status_code == 403:
        OUT["printer_limit_detail"] = body(r)
        break
OUT["printer_count"] = sql("SELECT COUNT(*) FROM printers")[0][0]

OUT["user_create"] = []
for i in range(4):
    r = a.post("/api/users", json={"username": f"certg-user-{i}", "email": f"certg-user-{i}@example.test",
                                   "password": "CertG-User-Password-1!", "role": "viewer"})
    OUT["user_create"].append(r.status_code)
    if r.status_code == 403:
        OUT["user_limit_detail"] = body(r)
        break
OUT["user_count"] = sql("SELECT COUNT(*) FROM users")[0][0]

def call(method, path, **kw):
    r = getattr(a, method)(path, **kw)
    return {"status": r.status_code, "body": body(r) if r.status_code >= 400 else None}

F = OUT["features"] = {}
# Pro "usage reports"
F["usage_reports"] = call("get", "/api/education/usage-report?days=30")
# Education: job approval, user groups, quotas
F["job_approval"] = call("put", "/api/config/require-job-approval", json={"enabled": True})
F["user_groups"] = call("get", "/api/groups")
F["print_quotas"] = call("get", "/api/admin/quotas")
# Production features the UI gates as Pro (frontend ProGate feature=orders/products/analytics/maintenance/branding)
F["orders"] = call("post", "/api/orders", json={"order_number": "CERTG-1", "customer_name": "Cert"})
F["products"] = call("post", "/api/products", json={"name": "CertG product"})
F["analytics"] = call("get", "/api/analytics")
F["maintenance"] = call("post", "/api/maintenance/tasks", json={"name": "CertG task", "interval_print_hours": 10})
F["branding"] = call("put", "/api/branding", json={"app_name": "CertG Farm"})
F["webhooks"] = call("get", "/api/webhooks")
# Enterprise: MFA, IP allowlist, organizations, GDPR, compliance (audit export)
F["mfa"] = call("post", "/api/auth/mfa/setup")
F["require_mfa"] = call("put", "/api/config/require-mfa", json={"require_mfa": False})
F["ip_allowlist"] = call("put", "/api/config/ip-allowlist", json={"enabled": False, "cidrs": ["10.0.0.0/8"]})
F["organizations"] = call("post", "/api/orgs", json={"name": "CertG Org " + tier})
F["gdpr_export"] = call("get", f"/api/users/{admin_id}/export")
# Org-level branding is branding too (review MEDIUM 2); other org settings stay free.
org_row = sql("SELECT id FROM groups WHERE is_org IS TRUE ORDER BY id")
F["org_branding"] = call("put", f"/api/orgs/{org_row[0][0]}/settings", json={"branding_app_name": "CertG white label"})
F["org_branding_logo"] = call("put", f"/api/orgs/{org_row[0][0]}/settings", json={"branding_logo_url": "/static/branding/x.png"})
F["org_settings_free"] = call("put", f"/api/orgs/{org_row[0][0]}/settings", json={"quiet_hours_enabled": True})
F["org_branding_clear"] = call("put", f"/api/orgs/{org_row[0][0]}/settings", json={"branding_app_name": None})
F["audit_export"] = call("get", "/api/export/audit-logs")
# Further members of each gated route family (cert-fix-G regression coverage)
F["analytics_failures"] = call("get", "/api/analytics/failures?days=30")
F["analytics_time_accuracy"] = call("get", "/api/analytics/time-accuracy?days=30")
F["consumables"] = call("post", "/api/consumables", json={"name": "CertG resin"})
F["maintenance_seed"] = call("post", "/api/maintenance/seed-defaults")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
F["branding_logo"] = call("post", "/api/branding/logo", files={"file": ("logo.png", PNG, "image/png")})
# Reads of existing records, the public branding read and the free dashboard stats stay ungated
F["orders_list"] = call("get", "/api/orders")
F["products_list"] = call("get", "/api/products")
F["maintenance_tasks_list"] = call("get", "/api/maintenance/tasks")
F["maintenance_status"] = call("get", "/api/maintenance/status")
F["branding_read"] = {"status": client().get("/api/branding").status_code, "body": None}
F["stats"] = call("get", "/api/stats")
F["audit_log_view"] = call("get", "/api/audit-logs")

# MQTT event republish (Enterprise): real republish path; only the external broker client is replaced.
from modules.notifications import mqtt_republish as MR
PUBLISHED = []
class _Broker:
    def publish(self, topic, msg, qos=0, retain=False): PUBLISHED.append(topic)
MR._get_client = lambda: _Broker()
MR._get_config = lambda: {"host": "broker.example.test", "port": 1883, "username": "", "password": "",
                          "topic_prefix": "odin", "use_tls": False}
MR.republish_alert(alert_type="print_failed", severity="error", title="t", message="m", printer_id=1, printer_name="p")
MR.republish_telemetry(1, "p", {"state": "IDLE"})
OUT["mqtt_republished"] = len(PUBLISHED)

# OIDC auto-provisioning (SSO). Identity provider is the external boundary, so
# the real provisioning function is driven with already-verified claims.
from core.db import SessionLocal
from modules.organizations.routes_oidc import _resolve_oidc_user
org_id = sql("SELECT id FROM groups WHERE is_org IS TRUE ORDER BY id")
if not org_id:
    sql("INSERT INTO groups(name,is_org) VALUES('CertG OIDC org',1)")
    org_id = sql("SELECT id FROM groups WHERE is_org IS TRUE ORDER BY id")
gid = org_id[0][0]
before = sql("SELECT COUNT(*) FROM users")[0][0]
db = SessionLocal()
try:
    res = _resolve_oidc_user(db, {"display_name": "IdP", "auto_create_users": True, "default_role": "viewer",
                                  "default_group_id": gid},
                             {"iss": "https://idp.example.test", "sub": "certg-oidc-sub", "email": "oidc-new@example.test",
                              "email_verified": True}, {})
    db.commit()
    OUT["oidc_autocreate"] = {"created": True}
except Exception as e:
    db.rollback()
    OUT["oidc_autocreate"] = {"created": False, "error": type(e).__name__, "status": getattr(e, "status_code", None)}
finally:
    db.close()
OUT["oidc_user_delta"] = sql("SELECT COUNT(*) FROM users")[0][0] - before
'''

TIERS = ("community", "pro", "education", "enterprise")


@pytest.fixture(scope="module")
def tiers(tmp_path_factory):
    root = tmp_path_factory.mktemp("cert-g-tiers")
    out = {}
    for t in TIERS:
        branding = root / f"branding-{t}"  # logo uploads stay inside the probe's tmp dir
        branding.mkdir()
        out[t] = run_probe(f"tier-{t}", PROBE, root, {"CERT_G_TIER": t, "ODIN_BRANDING_DIRECTORY": str(branding)})
    return out


def ok(result):
    return 200 <= result["status"] < 300


def test_no_network_during_any_tier_probe(tiers):
    assert all(tiers[t]["_net_attempts"] == [] for t in TIERS)


# ---------- F20-20.1-01 Community ----------
def test_community_is_free_default_without_license_file(tiers):
    lic = tiers["community"]["license"]
    assert lic["tier"] == "community" and lic["valid"] is False
    assert lic["max_printers"] == 5 and lic["max_users"] == 1


def test_community_allows_5_printers_and_rejects_6th(tiers):
    c = tiers["community"]
    assert c["printer_create"] == [201] * 5 + [403]
    assert c["printer_count"] == 5
    assert "Printer limit reached (5)" in c["printer_limit_detail"]["detail"]


def test_community_rejects_second_user(tiers):
    c = tiers["community"]
    assert c["user_create"] == [403]
    assert c["user_count"] == 1
    assert "User limit reached (1)" in c["user_limit_detail"]["detail"]


def test_community_oidc_autoprovision_respects_one_user_limit(tiers):
    assert tiers["community"]["oidc_user_delta"] == 0


@pytest.mark.parametrize("feature", ["usage_reports", "job_approval", "user_groups", "print_quotas"])
def test_community_rejects_paid_backend_gated_features(tiers, feature):
    assert tiers["community"]["features"][feature]["status"] == 403


# ---------- F20-20.1-02 Pro ----------
def test_pro_unlimited_printers_and_users(tiers):
    p = tiers["pro"]
    assert p["license"]["max_printers"] is None and p["license"]["max_users"] is None
    assert p["printer_create"] == [201] * 12 and p["printer_count"] == 12
    assert all(s in (200, 201) for s in p["user_create"]) and len(p["user_create"]) == 4
    assert p["user_count"] == 5


@pytest.mark.parametrize("feature", ["usage_reports", "orders", "products", "analytics", "maintenance", "branding", "webhooks"])
def test_pro_has_production_features_and_usage_reports(tiers, feature):
    assert ok(tiers["pro"]["features"][feature]), tiers["pro"]["features"][feature]


@pytest.mark.parametrize("feature", ["orders", "products", "analytics", "maintenance", "branding"])
def test_community_api_rejects_pro_production_features(tiers, feature):
    assert tiers["community"]["features"][feature]["status"] == 403


# ---------- F20-20.1-03 Education ----------
@pytest.mark.parametrize("feature", ["job_approval", "user_groups", "print_quotas"])
def test_education_features_off_on_pro_on_for_education(tiers, feature):
    assert tiers["pro"]["features"][feature]["status"] == 403
    assert "Education" in tiers["pro"]["features"][feature]["body"]["detail"]
    assert ok(tiers["education"]["features"][feature]), tiers["education"]["features"][feature]


def test_education_includes_pro_features(tiers):
    for feature in ("usage_reports", "orders", "products", "analytics", "maintenance", "branding", "webhooks"):
        assert ok(tiers["education"]["features"][feature]), feature
    assert set(tiers["pro"]["license"]["features"]) < set(tiers["education"]["license"]["features"])


def test_education_caps_are_9999_not_unlimited(tiers):
    """Records the 'Pro +' discrepancy: Pro is unlimited (null), Education/Enterprise default to 9999."""
    for t in ("education", "enterprise"):
        assert tiers[t]["license"]["max_printers"] == 9999 and tiers[t]["license"]["max_users"] == 9999
        assert tiers[t]["printer_create"] == [201] * 12


# ---------- F20-20.1-04 Enterprise ----------
ENTERPRISE = ["mfa", "require_mfa", "ip_allowlist", "organizations", "gdpr_export", "audit_export"]


@pytest.mark.parametrize("feature", ENTERPRISE)
def test_enterprise_has_mfa_ip_allowlist_orgs_gdpr_compliance(tiers, feature):
    assert ok(tiers["enterprise"]["features"][feature]), tiers["enterprise"]["features"][feature]


def test_enterprise_includes_pro_and_education_features(tiers):
    for feature in ("usage_reports", "orders", "products", "job_approval", "user_groups", "print_quotas"):
        assert ok(tiers["enterprise"]["features"][feature]), feature


FREE_SECURITY = ["mfa", "require_mfa", "ip_allowlist", "organizations", "gdpr_export"]


@pytest.mark.parametrize("feature", FREE_SECURITY)
def test_security_features_are_free_on_every_tier(tiers, feature):
    """Decision 2026-10-14: MFA, IP allowlist, organizations and GDPR export are included in every tier."""
    for t in TIERS:
        assert ok(tiers[t]["features"][feature]), (t, tiers[t]["features"][feature])


# ---------- tier gates (decision 2026-10-14: Pro gates enforced in the backend) ----------
PRO_GATED = {
    "orders": "orders", "products": "products", "consumables": "products",
    "analytics": "analytics", "analytics_failures": "analytics", "analytics_time_accuracy": "analytics",
    "maintenance": "maintenance", "maintenance_seed": "maintenance",
    "branding": "branding", "branding_logo": "branding",
    "org_branding": "branding", "org_branding_logo": "branding",
}


@pytest.mark.parametrize("call_name", sorted(PRO_GATED))
def test_pro_gate_community_403_pro_education_enterprise_allowed(tiers, call_name):
    c = tiers["community"]["features"][call_name]
    assert c["status"] == 403, c
    assert c["body"]["detail"] == (f"Feature '{PRO_GATED[call_name]}' requires Pro or Education or Enterprise. "
                                   "Current tier: community")
    for t in ("pro", "education", "enterprise"):
        assert ok(tiers[t]["features"][call_name]), (t, tiers[t]["features"][call_name])


def test_enterprise_gate_audit_export(tiers):
    for t in ("community", "pro", "education"):
        r = tiers[t]["features"]["audit_export"]
        assert r["status"] == 403 and r["body"]["detail"] == f"Feature 'audit_export' requires Enterprise. Current tier: {t}", (t, r)
    assert ok(tiers["enterprise"]["features"]["audit_export"])
    # The audit log viewer itself is not the export entitlement.
    assert all(ok(tiers[t]["features"]["audit_log_view"]) for t in TIERS)


def test_enterprise_gate_mqtt_event_republish(tiers):
    assert {t: tiers[t]["mqtt_republished"] for t in TIERS} == {"community": 0, "pro": 0, "education": 0, "enterprise": 2}


@pytest.mark.parametrize("call_name", ["org_settings_free", "org_branding_clear", "orders_list", "products_list", "maintenance_tasks_list", "maintenance_status",
                                       "branding_read", "stats"])
def test_reads_stay_available_on_every_tier(tiers, call_name):
    for t in TIERS:
        assert ok(tiers[t]["features"][call_name]), (t, tiers[t]["features"][call_name])


def test_pro_oidc_autoprovision_is_not_capped(tiers):
    assert tiers["pro"]["oidc_user_delta"] == 1 and tiers["pro"]["oidc_autocreate"] == {"created": True}


def test_community_oidc_autoprovision_refusal_is_a_coded_identity_error(tiers):
    assert tiers["community"]["oidc_autocreate"] == {"created": False, "error": "OIDCIdentityError", "status": None}


# ---------- downgrade / expiry keeps existing records ----------
DOWNGRADE = r'''
admin_id = seed_user("cert-g-admin", "admin")
install_license("pro")
a = login("cert-g-admin")
o = a.post("/api/orders", json={"order_number": "CERTG-DG-1", "customer_name": "Cert"}).json()["id"]
o2 = a.post("/api/orders", json={"order_number": "CERTG-DG-2", "customer_name": "Cert"}).json()["id"]
p = a.post("/api/products", json={"name": "CertG downgrade product"}).json()["id"]
p2 = a.post("/api/products", json={"name": "CertG downgrade product 2"}).json()["id"]
m = a.post("/api/maintenance/tasks", json={"name": "CertG downgrade task", "interval_print_hours": 10}).json()["id"]
OUT["pro_branding"] = a.put("/api/branding", json={"app_name": "CertG Downgrade Farm"}).status_code
mode = os.environ["CERT_G_DOWNGRADE"]
install_license(None) if mode == "removed" else install_license("pro", expires_at="2001-01-01")
OUT["tier_after"] = body(client().get("/api/license"))
R = OUT["after"] = {}
R["order_get"] = a.get(f"/api/orders/{o}").status_code
R["orders_list"] = [x["id"] for x in a.get("/api/orders").json()]
R["product_get"] = a.get(f"/api/products/{p}").status_code
R["products_list"] = sorted(x["id"] for x in a.get("/api/products").json())
R["maint_list"] = [x["id"] for x in a.get("/api/maintenance/tasks").json()]
R["branding_app_name"] = client().get("/api/branding").json().get("app_name")
R["order_patch"] = a.patch(f"/api/orders/{o}", json={"notes": "x"}).status_code
R["product_patch"] = a.patch(f"/api/products/{p}", json={"name": "renamed"}).status_code
R["maint_patch"] = a.patch(f"/api/maintenance/tasks/{m}", json={"name": "renamed"}).status_code
R["order_create"] = a.post("/api/orders", json={"order_number": "CERTG-DG-3", "customer_name": "Cert"}).status_code
R["branding_put"] = a.put("/api/branding", json={"app_name": "Other"}).status_code
R["order_delete"] = a.delete(f"/api/orders/{o2}").status_code
R["product_delete"] = a.delete(f"/api/products/{p2}").status_code
R["maint_delete"] = a.delete(f"/api/maintenance/tasks/{m}").status_code
R["counts"] = {"orders": sql("SELECT COUNT(*) FROM orders")[0][0], "products": sql("SELECT COUNT(*) FROM products")[0][0]}
OUT["ids"] = {"order": o, "product": p}
'''


@pytest.fixture(scope="module", params=["removed", "expired"])
def downgraded(request, tmp_path_factory):
    root = tmp_path_factory.mktemp("cert-g-downgrade")
    return run_probe(f"downgrade-{request.param}", DOWNGRADE, root, {"CERT_G_DOWNGRADE": request.param})


def test_downgraded_install_keeps_reading_and_deleting_its_records(downgraded):
    assert downgraded["pro_branding"] == 200
    assert downgraded["tier_after"]["valid"] is False and downgraded["tier_after"]["max_users"] == 1
    r, ids = downgraded["after"], downgraded["ids"]
    assert r["order_get"] == 200 and ids["order"] in r["orders_list"]
    assert r["product_get"] == 200 and ids["product"] in r["products_list"]
    assert len(r["maint_list"]) == 1
    assert r["branding_app_name"] == "CertG Downgrade Farm"
    assert (r["order_delete"], r["product_delete"], r["maint_delete"]) == (204, 204, 204)
    assert r["counts"] == {"orders": 1, "products": 1}


def test_downgraded_install_cannot_create_or_change_pro_records(downgraded):
    r = downgraded["after"]
    assert (r["order_patch"], r["product_patch"], r["maint_patch"], r["order_create"], r["branding_put"]) == (403,) * 5
