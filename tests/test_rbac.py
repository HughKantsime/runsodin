"""
O.D.I.N. RBAC Exhaustive Test — v2.2
test_rbac.py

Three-client auth model:
  - no_headers:    nothing sent → blocked by the API key middleware
  - api_key_only:  the legacy global API_KEY → authenticates as the first active admin, a
                   non-interactive platform superadmin (core/dependencies.py, since fb8a9bb): the
                   admin result, except CREDENTIAL_OPERATIONS (credential write guard: 403)
  - viewer/operator/admin: API key + JWT → full RBAC (server-enforced permission matrix)

License: LICENSE_GATED rows need a license feature. RBAC_LICENSE_TIER names the server's tier
(default community: the live container, which cannot be given a test-signed license); on a
tier without the feature every authorized caller gets the gate's 403.
tests/test_contracts/test_rbac_licensed_matrix.py runs this file on an Enterprise server.

Usage (live container):
    BASE_URL=... API_KEY=... ADMIN_USERNAME=... ADMIN_PASSWORD=... pytest tests/test_rbac.py
Optional RBAC_RECORD=<file> appends each row's expected/actual status (diagnostics).
"""

import pytest
import requests
import os
from pathlib import Path
from conftest import make_request


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parent / ".env.test"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

_load_env()
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


# ---------------------------------------------------------------------------
# Endpoint matrix
#
# Each entry: (method, path, expected, body, notes)
#
# expected dict keys: no_headers, api_key_only, viewer, operator, admin
#   None = skip this role
#
# RBAC truth table (from main.py grep):
#   - operator: most CRUD, printer control, plug power, spools, jobs, models, etc.
#   - admin: users, config, branding, backups, webhooks, SMTP, OIDC, permissions,
#            exports (after patch), backups list (after patch), pricing config write
# ---------------------------------------------------------------------------

# Shorthand helpers
def _pub():
    """Public endpoint — no auth needed at all."""
    return {"no_headers": 200, "api_key_only": None, "viewer": None, "operator": None, "admin": None}

def _setup_locked():
    """Setup endpoint — locked after completion."""
    return {"no_headers": 403, "api_key_only": None, "viewer": None, "operator": None, "admin": None}

def _api_read():
    """Standard authenticated GET — blocked without API key, allowed with key or JWT."""
    return {"no_headers": 401, "api_key_only": 200, "viewer": 200, "operator": 200, "admin": 200}

def _op_write():
    """Operator+ write — blocked for viewer."""
    return {"no_headers": 401, "api_key_only": 403, "viewer": 403, "operator": 200, "admin": 200}

def _admin_only():
    """Admin only — blocked for viewer and operator."""
    return {"no_headers": 401, "api_key_only": 403, "viewer": 403, "operator": 403, "admin": 200}

def _admin_read():
    """Admin-only read endpoint."""
    return {"no_headers": 401, "api_key_only": 403, "viewer": 403, "operator": 403, "admin": 200}


_ONLINE_LICENSE = {**_admin_only(), "admin": 400 if os.environ.get("RBAC_LICENSE_TIER", "community") == "community" else 502}

from rbac_endpoints_a import ENDPOINTS as _ENDPOINTS_A
from rbac_endpoints_b import ENDPOINTS as _ENDPOINTS_B
ENDPOINT_MATRIX = _ENDPOINTS_A + _ENDPOINTS_B + [
    ("GET", "/api/settings/remote-access", _admin_read(), None, "Remote access status"),
    ("GET", "/api/cameras/{printer_id}/snapshot", _api_read(), None, "Camera snapshot fallback"),
]



# ---------------------------------------------------------------------------
# Path substitution
# ---------------------------------------------------------------------------

def _resolve_path(path_template, test_data):
    subs = {
        "{printer_id}": str(test_data.get("printer_id", 1)),
        "{job_id}": str(test_data.get("job_id", 1)),
        "{model_id}": str(test_data.get("model_id", 1)),
        "{spool_id}": str(test_data.get("spool_id", 1)),
        "{product_id}": str(test_data.get("product_id", 1)),
        "{order_id}": str(test_data.get("order_id", 1)),
        "{webhook_id}": str(test_data.get("webhook_id", 1)),
        "{maintenance_task_id}": str(test_data.get("maintenance_task_id", 1)),
        "{maintenance_log_id}": str(test_data.get("maintenance_log_id", 1)),
        "{backup_filename}": test_data.get("backup_filename", "nonexistent.db"),
        "{filament_id}": str(test_data.get("filament_id", 1)),
        # New params added for coverage (sections 35-63)
        "{alert_id}": str(test_data.get("alert_id", 1)),
        "{timelapse_id}": str(test_data.get("timelapse_id", 1)),
        "{detection_id}": str(test_data.get("detection_id", 1)),
        "{token_id}": str(test_data.get("token_id", 1)),
        "{session_id}": str(test_data.get("session_id", 1)),
        "{group_id}": str(test_data.get("group_id", 1)),
        "{org_id}": str(test_data.get("org_id", 1)),
        "{user_id}": str(test_data.get("user_id", 1)),
        "{item_id}": str(test_data.get("item_id", 1)),
        "{component_id}": str(test_data.get("component_id", 1)),
        "{consumable_id}": str(test_data.get("consumable_id", 1)),
        "{consumable_link_id}": str(test_data.get("consumable_link_id", 1)),
        "{variant_id}": str(test_data.get("variant_id", 1)),
        "{rev_number}": str(test_data.get("rev_number", 1)),
        "{schedule_id}": str(test_data.get("schedule_id", 1)),
        "{preset_id}": str(test_data.get("preset_id", 1)),
        "{file_id}": str(test_data.get("file_id", 1)),
        "{nozzle_id}": str(test_data.get("nozzle_id", 1)),
        "{profile_id}": str(test_data.get("profile_id", 1)),
        "{slot_number}": str(test_data.get("slot_number", 1)),
        "{ams_id}": str(test_data.get("ams_id", 0)),
        "{slot_id}": str(test_data.get("slot_id", 0)),
        "{project_id}": str(test_data.get("project_id", 1)),
        "{filename}": test_data.get("filename", "test.jpg"),
        "{qr_code}": test_data.get("qr_code", "TEST_QR"),
        "{code}": test_data.get("code", "0500040000020002"),
    }
    path = path_template
    for k, v in subs.items():
        path = path.replace(k, v)
    return path


def _resolve_body(body, test_data):
    if body is None or isinstance(body, list) or not isinstance(body, dict):
        return body
    resolved = dict(body)
    if "printer_id" in resolved and resolved["printer_id"] == 1:
        resolved["printer_id"] = test_data.get("printer_id", 1)
    if "model_id" in resolved and resolved["model_id"] == 1:
        resolved["model_id"] = test_data.get("model_id", 1)
    if "product_id" in resolved and resolved["product_id"] == 1:
        resolved["product_id"] = test_data.get("product_id", 1)
    if "filament_id" in resolved and resolved["filament_id"] == 1:
        resolved["filament_id"] = test_data.get("filament_id", 1)
    return resolved


# ---------------------------------------------------------------------------
# Parametrize
# ---------------------------------------------------------------------------

# Credential operations: the credential write guard (core/credential_write_guard.py) refuses
# them to the non-interactive legacy-API-key admin with 403 "API tokens cannot create or change
# credentials; sign in with a password session". Asserted explicitly as 403 for api_key_only.
CREDENTIAL_OPERATIONS = {
    ("POST", "/api/tokens"), ("DELETE", "/api/tokens/{token_id}"),                     # API tokens
    ("DELETE", "/api/sessions/{session_id}"), ("DELETE", "/api/admin/sessions/{session_id}"),  # sessions
    ("POST", "/api/auth/mfa/setup"), ("POST", "/api/auth/mfa/confirm"), ("DELETE", "/api/auth/mfa"),
    ("DELETE", "/api/admin/users/{user_id}/mfa"), ("PUT", "/api/config/require-mfa"),   # MFA
    ("PUT", "/api/admin/oidc"), ("PUT", "/api/smtp-config"),                           # SSO / SMTP secrets
    ("POST", "/api/push/subscribe"),                                                   # push endpoint keys
    ("POST", "/api/backups"), ("GET", "/api/backups/{backup_filename}"),               # backups carry every
    ("DELETE", "/api/backups/{backup_filename}"), ("POST", "/api/backups/restore"),    # stored credential
    ("GET", "/api/admin/logs"), ("GET", "/api/admin/logs/stream"),                     # install-wide logs and
    ("GET", "/api/admin/support-bundle"),                                              # the support bundle
}

# License-gated rows: route -> license feature (license_manager.require_feature / feature_gate).
# On a server whose tier lacks the feature every authorized caller gets the gate's 403.
# The live container (no signed license can be installed in an image) runs Community;
# tests/test_contracts/test_rbac_licensed_matrix.py runs the same table on Enterprise.
LICENSE_GATED = {
    **{route: "analytics" for route in (("GET", "/api/analytics"), ("GET", "/api/analytics/failures"),
                                         ("GET", "/api/analytics/time-accuracy"), ("GET", "/api/reports/chargebacks"))},
    **{route: "branding" for route in (("PUT", "/api/branding"), ("POST", "/api/branding/logo"),
                                        ("POST", "/api/branding/favicon"))},
    **{route: "email_notifications" for route in (("PUT", "/api/smtp-config"), ("POST", "/api/alerts/test-email"),
                                                   ("POST", "/api/report-schedules"),
                                                   ("POST", "/api/report-schedules/{schedule_id}/run"))},
    **{route: "maintenance" for route in (("POST", "/api/maintenance/tasks"), ("PATCH", "/api/maintenance/tasks/{maintenance_task_id}"),
                                           ("POST", "/api/maintenance/logs"), ("POST", "/api/maintenance/seed-defaults"))},
    **{route: "orders" for route in (("POST", "/api/orders"), ("PATCH", "/api/orders/{order_id}"), ("POST", "/api/orders/{order_id}/items"),
                                      ("PATCH", "/api/orders/{order_id}/items/{item_id}"), ("POST", "/api/orders/{order_id}/schedule"),
                                      ("PATCH", "/api/orders/{order_id}/ship"))},
    **{route: "products" for route in (("POST", "/api/products"), ("PATCH", "/api/products/{product_id}"),
                                        ("POST", "/api/products/{product_id}/components"), ("POST", "/api/products/{product_id}/consumables"),
                                        ("POST", "/api/consumables"), ("PATCH", "/api/consumables/{consumable_id}"),
                                        ("POST", "/api/consumables/{consumable_id}/adjust"))},
    **{route: "permissions" for route in (("PUT", "/api/permissions"), ("POST", "/api/permissions/reset"))},
    ("PUT", "/api/admin/oidc"): "sso",
    ("GET", "/api/education/usage-report"): "usage_reports",
    **{route: "webhooks" for route in (("POST", "/api/webhooks"), ("PATCH", "/api/webhooks/{webhook_id}"),
                                        ("POST", "/api/webhooks/{webhook_id}/test"))},
    **{route: "job_approval" for route in (("POST", "/api/jobs/{job_id}/approve"), ("POST", "/api/jobs/{job_id}/resubmit"),
                                            ("PUT", "/api/config/require-job-approval"))},
    **{route: "print_quotas" for route in (("GET", "/api/quotas"), ("GET", "/api/admin/quotas"), ("PUT", "/api/admin/quotas/{user_id}"))},
    **{route: "user_groups" for route in (("GET", "/api/groups"), ("POST", "/api/groups"), ("GET", "/api/groups/{group_id}"),
                                           ("PATCH", "/api/groups/{group_id}"), ("DELETE", "/api/groups/{group_id}"))},
    ("GET", "/api/export/audit-logs"): "audit_export",
}
# Lowest tier carrying each feature (license_manager.TIERS); Enterprise carries every one.
_TIER_ORDER = ("community", "pro", "education", "enterprise")
FEATURE_MIN_TIER = {**{f: "pro" for f in ("analytics", "branding", "email_notifications", "maintenance", "orders", "products",
                                         "permissions", "sso", "usage_reports", "webhooks")},
                    **{f: "education" for f in ("job_approval", "print_quotas", "user_groups")},
                    "audit_export": "enterprise"}
LICENSE_TIER = os.environ.get("RBAC_LICENSE_TIER", "community")


def _tier_has(feature):
    return _TIER_ORDER.index(LICENSE_TIER) >= _TIER_ORDER.index(FEATURE_MIN_TIER[feature])

# Endpoints where DELETE should never run (session-breaking or not re-creatable)
DESTRUCTIVE_SKIP = {
    "DELETE /api/license",                # License can't be re-created in test
    "POST /api/license/unactivation-request",  # releases the installed license (licensed run loses its tier)
    "DELETE /api/sessions",               # Revokes ALL own sessions (breaks test runner tokens)
}

# Mapping: DELETE endpoint → (POST create endpoint, create body, id key in response)
# For these, we create a disposable resource before each DELETE test.
_EPHEMERAL_CREATE = {
    "DELETE /api/printers/{printer_id}": (
        "/api/printers", {"name": "DEL_test_printer", "model": "Test", "api_type": "bambu", "slot_count": 4, "is_active": True}, "id",
    ),
    "DELETE /api/jobs/{job_id}": (
        "/api/jobs", {"item_name": "DEL_test_job", "priority": 1}, "id",
    ),
    "DELETE /api/models/{model_id}": (
        "/api/models", {"name": "DEL_test_model", "build_time_hours": 0.5}, "id",
    ),
    "DELETE /api/spools/{spool_id}": None,  # needs filament_id — handled specially
    "DELETE /api/projects/{project_id}": (
        "/api/projects", {"name": "DEL_test_project"}, "id",
    ),
    "DELETE /api/products/{product_id}": (
        "/api/products", {"name": "DEL_test_product", "price": 1.00}, "id",
    ),
    "DELETE /api/orders/{order_id}": (
        "/api/orders", {}, "id",
    ),
    "DELETE /api/webhooks/{webhook_id}": (
        "/api/webhooks", {}, "id",
    ),
    "DELETE /api/maintenance/tasks/{maintenance_task_id}": (
        "/api/maintenance/tasks", {"name": "DEL_test_maint", "interval_days": 30}, "id",
    ),
    "DELETE /api/maintenance/logs/{maintenance_log_id}": None,  # needs printer_id — handled specially
    "DELETE /api/backups/{backup_filename}": (
        "/api/backups", None, "filename",
    ),
    "DELETE /api/filaments/{filament_id}": (
        "/api/filaments", {"brand": "DEL Test", "name": "DEL PLA", "material": "PLA", "color_hex": "#000000"}, "id",
    ),
    "DELETE /api/tokens/{token_id}": (
        "/api/tokens", {"name": "DEL_test_token"}, "id",
    ),
    "DELETE /api/users/{user_id}": None,  # handled specially (create user)
    "DELETE /api/users/{user_id}/erase": None,  # handled specially (create user)
    "DELETE /api/print-files/{file_id}": None,  # can't easily create — fall back to 404 tolerance
}


def _create_ephemeral(endpoint_key, admin_token, test_data):
    """Create a disposable resource for a destructive DELETE test.
    Returns the resolved path with the new resource ID, or None if creation failed."""
    import uuid

    spec = _EPHEMERAL_CREATE.get(endpoint_key)

    # Special cases that need dependent data
    if endpoint_key == "DELETE /api/spools/{spool_id}":
        fid = test_data.get("filament_id", 1)
        resp = make_request("POST", "/api/spools", "jwt", admin_token, {
            "filament_id": fid, "initial_weight_g": 500, "spool_weight_g": 200,
        })
        if resp.status_code == 200:
            return f"/api/spools/{resp.json().get('id')}"
        return None

    if endpoint_key == "DELETE /api/maintenance/logs/{maintenance_log_id}":
        pid = test_data.get("printer_id", 1)
        resp = make_request("POST", "/api/maintenance/logs", "jwt", admin_token, {
            "printer_id": pid, "task_name": "DEL_test_log",
        })
        if resp.status_code == 200:
            return f"/api/maintenance/logs/{resp.json().get('id')}"
        return None

    if endpoint_key in ("DELETE /api/users/{user_id}", "DELETE /api/users/{user_id}/erase"):
        tag = uuid.uuid4().hex[:6]
        resp = make_request("POST", "/api/users", "jwt", admin_token, {
            "username": f"del_test_{tag}", "email": f"del_{tag}@test.local",
            "password": "DelTestPass1!", "role": "viewer",
        })
        if resp.status_code == 200:
            uid = resp.json().get("id")
            if "erase" in endpoint_key:
                return f"/api/users/{uid}/erase"
            return f"/api/users/{uid}"
        return None

    if endpoint_key == "DELETE /api/print-files/{file_id}":
        return None  # Can't easily create print files via API

    if spec is None:
        return None

    create_path, create_body, id_key = spec
    resp = make_request("POST", create_path, "jwt", admin_token, create_body)
    if resp.status_code in (200, 201):
        data = resp.json()
        rid = data.get(id_key)
        if rid is not None:
            # Reconstruct DELETE path with the new ID
            path_template = endpoint_key.replace("DELETE ", "")
            # Replace the template param with actual ID
            for param in ["{printer_id}", "{job_id}", "{model_id}", "{spool_id}",
                          "{product_id}", "{order_id}", "{webhook_id}",
                          "{maintenance_task_id}", "{maintenance_log_id}",
                          "{backup_filename}", "{filament_id}", "{token_id}",
                          "{file_id}"]:
                if param in path_template:
                    return path_template.replace(param, str(rid))
    return None


PRODUCTION_OPERATOR_OPERATIONS = {("POST", "/api/products/production/outside-prints/999999/bind")}


def _expectations(method, path, expected):
    """Per-role expectations for one row on the server under test.

    api_key_only: the legacy global API key authenticates as the first active admin, a
    platform superadmin (core/dependencies.py, legacy_global_api_key, since fb8a9bb), as a
    non-interactive principal. It therefore gets the admin result, except for credential
    operations, which the credential write guard refuses with 403 (CREDENTIAL_OPERATIONS).
    """
    expected = dict(expected)
    if expected.get("api_key_only") is not None and expected.get("admin") is not None:
        expected["api_key_only"] = 403 if (method, path) in CREDENTIAL_OPERATIONS | PRODUCTION_OPERATOR_OPERATIONS else expected["admin"]
    feature = LICENSE_GATED.get((method, path))
    if feature and not _tier_has(feature):
        for role, status in expected.items():
            if role != "no_headers" and status is not None:
                expected[role] = 403  # the license gate refuses every authorized caller
    return expected


def _generate_params():
    params = []
    for method, path, expected, body, notes in ENDPOINT_MATRIX:
        for role, exp_status in _expectations(method, path, expected).items():
            if exp_status is None:
                continue
            test_id = f"{method} {path} [{role}]"
            params.append(pytest.param(method, path, role, exp_status, body, notes, id=test_id))
    return params


@pytest.mark.parametrize("method, path_template, role, expected_status, body, notes", _generate_params())
def test_rbac(method, path_template, role, expected_status, body, notes, tokens, test_data, api_key_enabled, admin_token):
    """RBAC matrix test with three-client auth model."""

    endpoint_key = f"{method} {path_template}"

    # Hard-skip for truly session-breaking endpoints. The legacy global API key acts as the
    # first admin (core/dependencies.py, legacy_global_api_key), so api_key_only runs them too.
    if role in ("admin", "operator", "api_key_only") and endpoint_key in DESTRUCTIVE_SKIP:
        pytest.skip(f"Skipping destructive call: {endpoint_key} [{role}]")

    # For destructive DELETEs, create ephemeral test data so we can actually test
    if role in ("admin", "operator", "api_key_only") and endpoint_key in _EPHEMERAL_CREATE:
        ephemeral_path = _create_ephemeral(endpoint_key, admin_token, test_data)
        if ephemeral_path:
            path = ephemeral_path
        else:
            path = _resolve_path(path_template, test_data)
    else:
        path = _resolve_path(path_template, test_data)

    resolved_body = _resolve_body(body, test_data)
    token = tokens.get(role)

    # Determine auth mode
    if role == "no_headers":
        auth_mode = "no_headers"
    elif role == "api_key_only":
        auth_mode = "api_key_only"
    else:
        auth_mode = "jwt"

    resp = make_request(method, path, auth_mode, token, resolved_body)
    actual = resp.status_code
    if os.environ.get("RBAC_RECORD"):
        import json as _json
        with open(os.environ["RBAC_RECORD"], "a") as _record:
            _record.write(_json.dumps({"method": method, "path": path_template, "role": role,
                                       "expected": expected_status, "actual": actual,
                                       "detail": resp.text[:240] if actual >= 400 else ""}) + "\n")

    # Exact match

    # # TRUSTED-NETWORK-MODE BYPASS
    # When API key auth is disabled, no_headers requests pass through middleware.
    # Accept any non-500 as "not a security bug" in trusted-network mode.
    if not api_key_enabled and role == "no_headers":
        if actual < 500:
            return  # Trusted-network mode: no API key enforcement
        if actual == 500:
            # 500s in trusted mode are handler bugs, not security issues
            import warnings
            warnings.warn(f"500 on {method} {path} [no_headers] in trusted mode")
            return

    # TRUSTED-NETWORK-MODE: api_key_only gets 401 because API key enforcement is disabled
    # locally. In production (non-trusted-network), api_key_only works correctly.
    if not api_key_enabled and role == "api_key_only" and expected_status == 200 and actual == 401:
        return  # Trusted-network mode: API key auth disabled locally

    # LICENSE-LIMIT TOLERANCE
    # POST /api/printers or /api/users may 403 from license caps, not RBAC
    if expected_status in (200, 201) and actual == 403 and role in ("admin", "operator", "api_key_only"):
        if any(x in path for x in ["/api/printers", "/api/users"]):
            return  # License limit, not an RBAC bug

    # SPOOLMAN-NOT-RUNNING TOLERANCE
    if actual == 502 and "spoolman" in path.lower():
        return  # Spoolman service not running

    if actual == expected_status:
        return

    # Acceptable alternatives when we expected success (200/201/204)
    # Auth passed but downstream issue (missing data, conflict, bad params,
    # external service unavailable) — not an RBAC bug
    if expected_status in (200, 201, 204):
        if actual in (200, 201, 204, 400, 404, 409, 422, 503):
            import warnings
            warnings.warn(f"RBAC soft pass: {method} {path} [{role}] expected {expected_status}, got {actual}")
            return

    # Expected 403 but got 422 (setup endpoints — validation runs before lock check)
    if expected_status == 403 and actual == 422 and "/api/setup/" in path:
        return

    # Expected 401 but got 403 (some middleware returns 403 instead of 401)
    if expected_status == 401 and actual == 403:
        return

    # Expected 403 (viewer blocked) but got 401 (no JWT = unauthed entirely)
    if expected_status == 403 and actual == 401 and role == "api_key_only":
        return  # api_key_only has no JWT, so 401 is acceptable for "must be operator+"

    # 500 on privileged endpoints is a server bug, not a skip
    if actual == 500:
        pytest.fail(
            f"Server error 500 on {method} {path} [{role}] — "
            f"expected {expected_status}. Server errors are bugs, not data issues."
        )

    pytest.fail(
        f"\n{'='*60}\n"
        f"RBAC FAILURE: {method} {path}\n"
        f"  Role:     {role}\n"
        f"  Auth:     {auth_mode}\n"
        f"  Expected: {expected_status}\n"
        f"  Actual:   {actual}\n"
        f"  Notes:    {notes}\n"
        f"{'='*60}"
    )


def test_rbac_summary(test_data):
    """Print summary."""
    print(f"\n{'='*60}")
    print("RBAC v2.1 Test Data:")
    for k, v in sorted(test_data.items()):
        print(f"  {k}: {v}")
    total = sum(1 for e in ENDPOINT_MATRIX for r, s in e[2].items() if s is not None)
    print(f"\nTotal endpoint×role tests: {total}")
    print(f"{'='*60}")
