"""Cert F / cluster B — F11-11.3-01/03/04/05 role matrix on the real server (LiveOdin, enterprise license).

"Three roles: admin, operator, viewer"; "Viewer: read-only + submit jobs (with approval mode)";
"Operator: day-to-day production (create/edit jobs, spools, models, orders, products, maintenance)";
"Admin: full system control (users, settings, branding, SSO, webhooks, backups, license, pricing)".
"""
from __future__ import annotations

import pytest
import requests

from tests.test_contracts.test_cert_F_00_live import strong_password
from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("roles")).start()
    o.boot_admin()
    o.viewer = o.mkuser("role_viewer", "viewer")
    o.operator = o.mkuser("role_operator", "operator")
    r = o.post("/api/printers", json={"name": "Role printer", "api_type": "moonraker", "api_host": "192.0.2.31"}, headers=o.admin_bearer)
    assert r.status_code == 201, r.text
    o.printer_id = r.json()["id"]
    r = o.post("/api/filaments", json={"brand": "Cert", "name": "Role PLA", "material": "PLA"}, headers=o.operator["headers"])
    assert r.status_code == 200, r.text
    o.filament_id = r.json()["id"]
    yield o
    o.stop()


def call(o, method, path, headers, body=None):
    return requests.request(method, o.url + path, json=body, headers=headers, timeout=30)


# ------------------------------------------------------------------ F11-11.3-01
def test_rbac_policy_knows_exactly_three_roles(odin):
    perms = odin.get("/api/permissions").json()
    roles = {r for section in ("page_access", "action_access") for v in perms[section].values() for r in v}
    assert roles == {"admin", "operator", "viewer"}
    bad = {"page_access": {"dashboard": ["superuser"]}, "action_access": {}}
    assert odin.put("/api/permissions", json=bad, headers=odin.admin_bearer).status_code == 400
    for role in ("admin", "operator", "viewer"):
        assert odin.one("SELECT role FROM users WHERE role=:r", r=role) is not None


def test_user_api_rejects_roles_outside_the_three(odin):
    r = odin.post("/api/users", json={"username": "role_bogus", "email": "role_bogus@example.test",
                                      "password": strong_password(), "role": "superuser"}, headers=odin.admin_bearer)
    assert r.status_code in (400, 422)
    assert odin.one("SELECT 1 FROM users WHERE username='role_bogus'") is None


# ------------------------------------------------------------------ F11-11.3-03 viewer
READ_PATHS = ["/api/printers", "/api/jobs", "/api/spools", "/api/models", "/api/orders", "/api/products",
              "/api/maintenance/tasks", "/api/maintenance/logs", "/api/pricing-config"]


@pytest.mark.parametrize("path", READ_PATHS)
def test_viewer_can_read_production_data(odin, path):
    assert odin.get(path, headers=odin.viewer["headers"]).status_code == 200


def viewer_writes(o):
    return [
        ("POST", "/api/spools", {"filament_id": o.filament_id}, "spools"),
        ("POST", "/api/models", {"name": "Viewer model"}, "models"),
        ("POST", "/api/orders", {"order_number": "VIEWER-1"}, "orders"),
        ("POST", "/api/products", {"name": "Viewer product"}, "products"),
        ("POST", "/api/maintenance/tasks", {"name": "Viewer task"}, "maintenance_tasks"),
        ("POST", "/api/maintenance/logs", {"printer_id": o.printer_id, "task_name": "Viewer log"}, "maintenance_logs"),
        ("PATCH", f"/api/printers/{o.printer_id}", {"nickname": "viewer"}, None),
        ("POST", "/api/printers", {"name": "Viewer printer"}, "printers"),
        ("DELETE", f"/api/printers/{o.printer_id}", None, "printers"),
    ]


def test_viewer_writes_are_refused_without_side_effects(odin):
    for method, path, body, table in viewer_writes(odin):
        before = odin.one(f"SELECT COUNT(*) AS n FROM {table}").n if table else None
        r = call(odin, method, path, odin.viewer["headers"], body)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        if table:
            assert odin.one(f"SELECT COUNT(*) AS n FROM {table}").n == before, path
    assert odin.one("SELECT nickname FROM printers WHERE id=:i", i=odin.printer_id).nickname is None


def test_viewer_cannot_edit_approve_or_start_jobs(odin):
    r = odin.post("/api/jobs", json={"item_name": "Operator job for viewer"}, headers=odin.operator["headers"])
    job = r.json()["id"]
    assert odin.patch(f"/api/jobs/{job}", json={"notes": "viewer"}, headers=odin.viewer["headers"]).status_code == 403
    assert odin.post(f"/api/jobs/{job}/start", headers=odin.viewer["headers"]).status_code == 403
    assert odin.post(f"/api/jobs/{job}/approve", headers=odin.viewer["headers"]).status_code == 403
    assert odin.one("SELECT status, notes FROM jobs WHERE id=:i", i=job) == ("pending", None)


def test_viewer_submission_lands_pending_approval_when_approval_mode_is_on(odin):
    assert odin.put("/api/config/require-job-approval", json={"enabled": True}, headers=odin.admin_bearer).status_code == 200
    try:
        r = odin.post("/api/jobs", json={"item_name": "Viewer approval submission"}, headers=odin.viewer["headers"])
        assert r.status_code == 201, r.text
        row = odin.one("SELECT status, submitted_by FROM jobs WHERE id=:i", i=r.json()["id"])
        assert row.status == "submitted" and row.submitted_by == odin.viewer["id"]
        # An operator approves it into the queue; the viewer cannot.
        assert odin.post(f"/api/jobs/{r.json()['id']}/approve", headers=odin.viewer["headers"]).status_code == 403
        assert odin.post(f"/api/jobs/{r.json()['id']}/approve", headers=odin.operator["headers"]).status_code == 200
        assert odin.one("SELECT status FROM jobs WHERE id=:i", i=r.json()["id"]).status == "pending"
    finally:
        odin.put("/api/config/require-job-approval", json={"enabled": False}, headers=odin.admin_bearer)


def test_viewer_submission_enters_queue_directly_when_approval_mode_is_off(odin):
    """Evidence for the claim_mismatch: with approval off, a viewer's job is created as pending (not read-only)."""
    assert odin.get("/api/config/require-job-approval", headers=odin.viewer["headers"]).json() == {"require_job_approval": False}
    r = odin.post("/api/jobs", json={"item_name": "Viewer direct job"}, headers=odin.viewer["headers"])
    assert r.status_code == 201, r.text
    assert odin.one("SELECT status FROM jobs WHERE id=:i", i=r.json()["id"]).status == "pending"


# ------------------------------------------------------------------ F11-11.3-04 operator
def test_operator_creates_and_edits_production_records(odin):
    h = odin.operator["headers"]
    created = {}
    for name, path, body in [
        ("jobs", "/api/jobs", {"item_name": "Operator job"}),
        ("spools", "/api/spools", {"filament_id": odin.filament_id}),
        ("models", "/api/models", {"name": "Operator model"}),
        ("orders", "/api/orders", {"order_number": "OP-1"}),
        ("products", "/api/products", {"name": "Operator product"}),
        ("maintenance_tasks", "/api/maintenance/tasks", {"name": "Operator task"}),
        ("maintenance_logs", "/api/maintenance/logs", {"printer_id": odin.printer_id, "task_name": "Operator log"}),
    ]:
        r = odin.post(path, json=body, headers=h)
        assert r.status_code in (200, 201), (path, r.text)
        created[name] = r.json()["id"]
        assert odin.one(f"SELECT 1 FROM {name} WHERE id=:i", i=created[name]) is not None
    for path, body, table, column, expected in [
        (f"/api/jobs/{created['jobs']}", {"notes": "edited"}, "jobs", "notes", "edited"),
        (f"/api/spools/{created['spools']}", {"notes": "edited"}, "spools", "notes", "edited"),
        (f"/api/models/{created['models']}", {"notes": "edited"}, "models", "notes", "edited"),
        (f"/api/products/{created['products']}", {"description": "edited"}, "products", "description", "edited"),
        (f"/api/maintenance/tasks/{created['maintenance_tasks']}", {"description": "edited"}, "maintenance_tasks", "description", "edited"),
    ]:
        r = odin.patch(path, json=body, headers=h)
        assert r.status_code == 200, (path, r.text)
        assert odin.one(f"SELECT {column} AS v FROM {table} WHERE id=:i", i=int(path.rsplit('/', 1)[1])).v == expected
    odin.created = created


def test_operator_cannot_edit_orders_or_delete_products(odin):
    """Evidence for the claim_mismatch: order edits (and order-item edits, product deletion) are admin-only."""
    h = odin.operator["headers"]
    order = odin.post("/api/orders", json={"order_number": "OP-EDIT"}, headers=h).json()["id"]
    assert odin.patch(f"/api/orders/{order}", json={"notes": "operator edit"}, headers=h).status_code == 403
    assert odin.one("SELECT notes FROM orders WHERE id=:i", i=order).notes is None
    assert odin.patch(f"/api/orders/{order}", json={"notes": "admin edit"}, headers=odin.admin_bearer).status_code == 200
    product = odin.post("/api/products", json={"name": "Op delete product"}, headers=h).json()["id"]
    assert odin.delete(f"/api/products/{product}", headers=h).status_code == 403
    assert odin.delete(f"/api/orders/{order}", headers=h).status_code == 204


# ------------------------------------------------------------------ F11-11.3-05 admin
def admin_operations(o):
    return [
        ("users", "POST", "/api/users", {"username": "admin_made", "email": "admin_made@example.test", "password": strong_password(), "role": "viewer"},
         lambda: o.one("SELECT role FROM users WHERE username='admin_made'") is not None),
        ("settings", "PUT", "/api/config", {"spoolman_url": "http://192.0.2.77:7912"},
         lambda: o.get("/api/config", headers=o.admin_bearer).json()["spoolman_url"] == "http://192.0.2.77:7912"),
        ("settings", "PUT", "/api/config/require-mfa", {"require_mfa": False}, lambda: True),
        ("branding", "PUT", "/api/branding", {"app_name": "Cert B Brand"},
         lambda: o.get("/api/branding").json()["app_name"] == "Cert B Brand"),
        ("sso", "PUT", "/api/admin/oidc", {"provider_type": "generic", "client_id": "cert-b-client", "client_secret": "throwaway-" + strong_password(),
                                          "discovery_url": "https://idp.example.test/.well-known/openid-configuration", "is_enabled": False},
         lambda: o.get("/api/admin/oidc", headers=o.admin_bearer).json()["client_id"] == "cert-b-client"),
        ("webhooks", "POST", "/api/webhooks", {"name": "Cert B hook", "url": "https://hooks.example.test/cert-b", "webhook_type": "generic"},
         lambda: any(w["name"] == "Cert B hook" for w in o.get("/api/webhooks", headers=o.admin_bearer).json())),
        ("backups", "POST", "/api/backups", None,
         lambda: len(o.get("/api/backups", headers=o.admin_bearer).json()) >= 1),
        ("pricing", "PUT", "/api/pricing-config", {"hourly_rate": 42.5},
         lambda: o.get("/api/pricing-config", headers=o.admin_bearer).json()["hourly_rate"] == 42.5),
        ("approval", "PUT", "/api/config/require-job-approval", {"enabled": False}, lambda: True),
    ]


def test_viewer_and_operator_are_refused_on_admin_domains(odin):
    for domain, method, path, body, _ in admin_operations(odin):
        for who in (odin.viewer, odin.operator):
            r = call(odin, method, path, who["headers"], body)
            assert r.status_code == 403, (who["username"], domain, path, r.status_code)
    for path in ("/api/users", "/api/admin/oidc", "/api/webhooks", "/api/backups", "/api/admin/sessions", "/api/smtp-config"):
        for who in (odin.viewer, odin.operator):
            assert odin.get(path, headers=who["headers"]).status_code == 403, (who["username"], path)
    lic = odin.sign_license("enterprise", "Refused upload")
    for who in (odin.viewer, odin.operator):
        r = requests.post(odin.url + "/api/license/upload", files={"file": ("odin.license", lic.encode())}, headers=who["headers"], timeout=30)
        assert r.status_code == 403
    assert (odin.dir / "license" / "odin.license").read_text().strip() != lic
    assert odin.one("SELECT 1 FROM users WHERE username='admin_made'") is None


def test_admin_controls_every_system_domain(odin):
    for domain, method, path, body, verify in admin_operations(odin):
        r = call(odin, method, path, odin.admin_bearer, body)
        assert r.status_code in (200, 201), (domain, path, r.status_code, r.text[:300])
        assert verify(), domain
    oidc = odin.get("/api/admin/oidc", headers=odin.admin_bearer).json()
    assert "throwaway-" not in str(oidc)
    lic = odin.sign_license("enterprise", "Cert B Uploaded")
    r = requests.post(odin.url + "/api/license/upload", files={"file": ("odin.license", lic.encode())}, headers=odin.admin_bearer, timeout=30)
    assert r.status_code == 200, r.text
    assert (odin.dir / "license" / "odin.license").read_text().strip() == lic
    assert odin.get("/api/license").json()["tier"] == "enterprise" and odin.get("/api/license").json()["valid"] is True


def test_admin_user_edit_and_hard_delete_retire_credentials(odin):
    target = odin.mkuser("role_deleted", "viewer")
    assert odin.patch(f"/api/users/{target['id']}", json={"role": "operator"}, headers=odin.admin_bearer).status_code == 200
    assert odin.one("SELECT role FROM users WHERE id=:i", i=target["id"]).role == "operator"
    jti_rows = odin.all("SELECT token_jti FROM active_sessions WHERE user_id=:i", i=target["id"])
    assert jti_rows
    r = odin.delete(f"/api/users/{target['id']}", headers=odin.admin_bearer)
    assert r.status_code == 200, r.text
    assert odin.one("SELECT 1 FROM users WHERE id=:i", i=target["id"]) is None
    for row in jti_rows:
        assert odin.one("SELECT 1 FROM token_blacklist WHERE jti=:j", j=row.token_jti) is not None
    assert odin.get("/api/jobs", headers=target["headers"]).status_code == 401
    # Username reuse does not inherit the deleted account's token.
    odin.mkuser("role_deleted", "admin", login=False)
    assert odin.get("/api/users", headers=target["headers"]).status_code == 401
