"""

# Unique suffix for this test run to avoid username collisions
_TEST_RUN_ID = uuid.uuid4().hex[:6]
O.D.I.N. Test Suite — Shared Fixtures (v2.1)
conftest.py

Three-client auth model:
  1. no_headers    — sends nothing (should be blocked by API key middleware)
  2. api_key_only  — sends API key but no JWT (SPA perimeter, read-only)
  3. jwt(role)     — sends API key + JWT (full RBAC)

Usage:
    pip install pytest requests pytest-html --break-system-packages
    pytest tests/test_rbac.py -v --tb=short 2>&1 | tee rbac_v2_results.txt
"""

import os
import uuid
import pytest
import requests
from pathlib import Path
from helpers import login as _shared_login

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
API_KEY = os.environ.get("API_KEY", "")
ADMIN_USERNAME = os.environ["ADMIN_USERNAME"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

TEST_VIEWER_USER = os.environ.get("TEST_VIEWER_USERNAME", "test_viewer_rbac")
TEST_VIEWER_PASS = os.environ.get("TEST_VIEWER_PASSWORD", "ViewerTestPass1!")
TEST_OPERATOR_USER = os.environ.get("TEST_OPERATOR_USERNAME", "test_operator_rbac")
TEST_OPERATOR_PASS = os.environ.get("TEST_OPERATOR_PASSWORD", "OperatorTestPass1!")
TEST_ADMIN_USER = os.environ.get("TEST_ADMIN_USERNAME", "test_admin_rbac")
TEST_ADMIN_PASS = os.environ.get("TEST_ADMIN_PASSWORD", "AdminTestPass1!")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _login(username, password):
    return _shared_login(BASE_URL, username, password, api_key=API_KEY or None)


def make_request(method, path, auth_mode, token=None, body=None):
    """
    Make an API request with one of three auth modes:
      - "no_headers":   sends nothing
      - "api_key_only": sends API key header only (no JWT)
      - "jwt":          sends API key + JWT Bearer token
    """
    url = f"{BASE_URL}{path}"
    headers = {}

    if auth_mode == "api_key_only":
        if API_KEY:
            headers["X-API-Key"] = API_KEY
    elif auth_mode == "jwt":
        if API_KEY:
            headers["X-API-Key"] = API_KEY
        if token:
            headers["Authorization"] = f"Bearer {token}"
    # "no_headers" sends nothing

    return requests.request(method, url, json=body, headers=headers, timeout=15)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


def _restart_backend():
    """Restart backend process to clear in-memory rate limits and lockouts."""
    import subprocess, time
    try:
        subprocess.run(
            ["docker", "exec", "odin", "supervisorctl", "restart", "backend"],
            capture_output=True, timeout=15,
        )
        time.sleep(3)  # Wait for backend to come back up
    except Exception:
        pass  # Best-effort; may fail if not running in Docker


@pytest.fixture(scope="session", autouse=True)
def _clear_rate_limits():
    """Restart backend before and after tests to clear in-memory rate limits and lockouts.
    Prevents stale lockouts from prior runs from causing fixture failures.
    """
    _restart_backend()
    yield
    _restart_backend()


@pytest.fixture(scope="session")
def api_key_enabled():
    """Detect whether API key auth is enforced.
    If both requests (with and without API key) return 200 on /health,
    the server is in trusted-network mode (api_key is None).
    """
    import requests as req
    r1 = req.get(f"{BASE_URL}/health", timeout=10)
    r2 = req.get(f"{BASE_URL}/health",
                  headers={"X-API-Key": "definitely-wrong-key"}, timeout=10)
    enabled = not (r1.status_code == 200 and r2.status_code == 200)
    if not enabled:
        print("\n  ⚠ API key auth DISABLED — trusted-network mode detected")
    return enabled

@pytest.fixture(scope="session")
def admin_token():
    token = _login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert token, f"Failed to login as admin ({ADMIN_USERNAME})"
    return token


@pytest.fixture(scope="session")
def test_users(admin_token):
    users = {}
    created_ids = []

    for username, password, role in [
        (TEST_VIEWER_USER, TEST_VIEWER_PASS, "viewer"),
        (TEST_OPERATOR_USER, TEST_OPERATOR_PASS, "operator"),
        (TEST_ADMIN_USER, TEST_ADMIN_PASS, "admin"),
    ]:
        resp = make_request("POST", "/api/users", "jwt", admin_token, {
            "username": username,
            "email": f"{username}@test.local",
            "password": password,
            "role": role,
        })
        if resp.status_code == 200:
            uid = resp.json().get("id")
            if uid:
                created_ids.append(uid)

        token = _login(username, password)

        # If login fails, user may exist with stale password — reset it
        if not token:
            # Find user ID and update password
            users_resp = make_request("GET", "/api/users", "jwt", admin_token)
            if users_resp.status_code == 200:
                for u in users_resp.json():
                    if u.get("username") == username:
                        make_request("PATCH", f"/api/users/{u['id']}", "jwt", admin_token, {
                            "password": password,
                        })
                        token = _login(username, password)
                        break

        assert token, f"Failed to login as {role} ({username})"
        users[role] = token

    yield users

    for uid in created_ids:
        make_request("DELETE", f"/api/users/{uid}", "jwt", admin_token)


@pytest.fixture(scope="session")
def tokens(admin_token, test_users):
    """Dict of role → token. Special keys 'no_headers' and 'api_key_only' have no token."""
    return {
        "no_headers": None,
        "api_key_only": None,
        "viewer": test_users["viewer"],
        "operator": test_users["operator"],
        "admin": test_users["admin"],
    }


@pytest.fixture(scope="session")
def test_data(admin_token):
    data = {}
    cleanup = []

    # --- Filament ---
    resp = make_request("POST", "/api/filaments", "jwt", admin_token, {
        "brand": "RBAC Test", "name": "RBAC PLA", "material": "PLA", "color_hex": "#FF0000",
    })
    if resp.status_code == 200:
        fid = resp.json().get("id")
        data["filament_id"] = fid
        cleanup.append(("DELETE", f"/api/filaments/{fid}"))

    # --- Printer ---
    resp = make_request("POST", "/api/printers", "jwt", admin_token, {
        "name": "RBAC Test Printer", "model": "Bambu Lab X1C",
        "api_type": "bambu", "slot_count": 4, "is_active": True,
    })
    if resp.status_code in (200, 201):
        pid = resp.json().get("id")
        data["printer_id"] = pid
        cleanup.append(("DELETE", f"/api/printers/{pid}"))
    else:
        # POST /api/printers may require admin - try to find existing
        resp2 = make_request("GET", "/api/printers", "jwt", admin_token)
        if resp2.status_code == 200:
            printers = resp2.json()
            if isinstance(printers, list) and printers:
                data["printer_id"] = printers[0].get("id", 1)

    # --- Model ---
    resp = make_request("POST", "/api/models", "jwt", admin_token, {
        "name": "RBAC Test Model", "build_time_hours": 1.5,
    })
    if resp.status_code in (200, 201):
        mid = resp.json().get("id")
        data["model_id"] = mid
        cleanup.append(("DELETE", f"/api/models/{mid}"))

    # --- Spool ---
    if "filament_id" in data:
        resp = make_request("POST", "/api/spools", "jwt", admin_token, {
            "filament_id": data["filament_id"], "initial_weight_g": 1000, "spool_weight_g": 250,
        })
        if resp.status_code == 200:
            sid = resp.json().get("id")
            data["spool_id"] = sid
            cleanup.append(("DELETE", f"/api/spools/{sid}"))

    # --- Job ---
    resp = make_request("POST", "/api/jobs", "jwt", admin_token, {
        "item_name": "RBAC Test Job", "priority": 3,
    })
    if resp.status_code in (200, 201):
        jid = resp.json().get("id")
        data["job_id"] = jid
        cleanup.append(("DELETE", f"/api/jobs/{jid}"))

    # --- Product ---
    resp = make_request("POST", "/api/products", "jwt", admin_token, {
        "name": "RBAC Test Product", "price": 10.00,
    })
    if resp.status_code in (200, 201):
        prid = resp.json().get("id")
        data["product_id"] = prid
        cleanup.append(("DELETE", f"/api/products/{prid}"))

    # --- Order ---
    resp = make_request("POST", "/api/orders", "jwt", admin_token, {})
    if resp.status_code in (200, 201):
        oid = resp.json().get("id")
        data["order_id"] = oid
        cleanup.append(("DELETE", f"/api/orders/{oid}"))

    # --- Webhook ---
    resp = make_request("POST", "/api/webhooks", "jwt", admin_token, {})
    if resp.status_code == 200:
        wid = resp.json().get("id")
        data["webhook_id"] = wid
        cleanup.append(("DELETE", f"/api/webhooks/{wid}"))

    # --- Maintenance task ---
    resp = make_request("POST", "/api/maintenance/tasks", "jwt", admin_token, {
        "name": "RBAC Test Maintenance", "interval_days": 30,
    })
    if resp.status_code == 200:
        mtid = resp.json().get("id")
        data["maintenance_task_id"] = mtid
        cleanup.append(("DELETE", f"/api/maintenance/tasks/{mtid}"))

    # --- Maintenance log ---
    if "printer_id" in data:
        resp = make_request("POST", "/api/maintenance/logs", "jwt", admin_token, {
            "printer_id": data["printer_id"], "task_name": "RBAC Test Log",
        })
        if resp.status_code == 200:
            mlid = resp.json().get("id")
            data["maintenance_log_id"] = mlid
            cleanup.append(("DELETE", f"/api/maintenance/logs/{mlid}"))

    # --- Backup ---
    resp = make_request("POST", "/api/backups", "jwt", admin_token)
    if resp.status_code == 200:
        bfn = resp.json().get("filename")
        data["backup_filename"] = bfn
        if bfn:
            cleanup.append(("DELETE", f"/api/backups/{bfn}"))

    yield data

    for method, path in reversed(cleanup):
        try:
            make_request(method, path, "jwt", admin_token)
        except Exception:
            pass
