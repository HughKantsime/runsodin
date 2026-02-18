"""
O.D.I.N. Layer 3 — Security Test Fixtures
conftest.py for tests/security/

Independent from root conftest.py to avoid RBAC collection conflicts.
Follows the pattern from test_security.py: loads .env.test, provides
session-scoped auth tokens for admin/viewer/operator.
Creates test users if they don't exist.
"""

import os
import time
import pytest
import requests
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parents[1] / ".env.test"
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


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _headers(token=None):
    """Build request headers with optional JWT and API key."""
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _auth_headers(token):
    """Build auth headers WITHOUT Content-Type (for multipart uploads)."""
    h = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _no_auth_headers():
    """Headers with NO api key and NO JWT."""
    return {"Content-Type": "application/json"}


def _login(username, password):
    """Login and return JWT token, or None on failure."""
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        data={"username": username, "password": password},
        headers=headers,
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("access_token") or data.get("token")
    return None


def _ensure_user(admin_token, username, password, role):
    """Create a test user if it doesn't exist. Returns JWT or None."""
    token = _login(username, password)
    if token:
        return token

    # Try to create
    resp = requests.post(
        f"{BASE_URL}/api/users",
        json={
            "username": username,
            "email": f"{username}@test.local",
            "password": password,
            "role": role,
        },
        headers=_headers(admin_token),
        timeout=10,
    )
    if resp.status_code in (200, 201):
        time.sleep(0.5)
        return _login(username, password)

    # User may exist with different password — try to reset it
    if resp.status_code in (400, 409):
        users_resp = requests.get(
            f"{BASE_URL}/api/users",
            headers=_headers(admin_token),
            timeout=10,
        )
        if users_resp.status_code == 200:
            for u in users_resp.json():
                if u.get("username") == username:
                    requests.patch(
                        f"{BASE_URL}/api/users/{u['id']}",
                        json={"password": password},
                        headers=_headers(admin_token),
                        timeout=10,
                    )
                    time.sleep(0.5)
                    return _login(username, password)
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def admin_token():
    token = _login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert token, f"Failed to login as admin ({ADMIN_USERNAME})"
    return token


@pytest.fixture(scope="session")
def viewer_token(admin_token):
    token = _ensure_user(admin_token, TEST_VIEWER_USER, TEST_VIEWER_PASS, "viewer")
    if not token:
        pytest.skip(f"Viewer test user ({TEST_VIEWER_USER}) not available and could not be created")
    return token


@pytest.fixture(scope="session")
def operator_token(admin_token):
    token = _ensure_user(admin_token, TEST_OPERATOR_USER, TEST_OPERATOR_PASS, "operator")
    if not token:
        pytest.skip(f"Operator test user ({TEST_OPERATOR_USER}) not available and could not be created")
    return token


@pytest.fixture(scope="session")
def api_key_enabled():
    """Detect whether API key auth is enforced."""
    r1 = requests.get(f"{BASE_URL}/api/health", timeout=10)
    r2 = requests.get(
        f"{BASE_URL}/api/health",
        headers={"X-API-Key": "definitely-wrong-key"},
        timeout=10,
    )
    return not (r1.status_code == 200 and r2.status_code == 200)
