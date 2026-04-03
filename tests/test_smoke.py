"""
O.D.I.N. API Smoke Tests — GET endpoint health check.

Hits every GET endpoint with admin authentication and asserts no 500 errors.
This catches broken imports, missing tables, and crash-on-render bugs.

Run: pytest tests/test_smoke.py -v --tb=short
Requires: running O.D.I.N. container (make build)
"""

import os
import pytest
import requests
from pathlib import Path

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
                os.environ[key.strip()] = value.strip()

_load_env()

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _login(username, password):
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


# ---------------------------------------------------------------------------
# All GET endpoints (parameterized path, some use placeholder IDs)
# ---------------------------------------------------------------------------

# Endpoints that take no path params or use query params only
GET_ENDPOINTS = [
    # Auth
    "/api/auth/me",
    "/api/auth/capabilities",
    "/api/auth/me/theme",
    "/api/auth/mfa/status",
    "/api/config/require-mfa",
    # Printers
    "/api/printers",
    "/api/printers/tags",
    "/api/printers/live-status",
    # Jobs
    "/api/jobs",
    "/api/jobs/filament-check",
    "/api/jobs/failure-reasons",
    "/api/config/require-job-approval",
    "/api/print-jobs",
    "/api/print-jobs/stats",
    "/api/print-jobs/unlinked",
    "/api/presets",
    # Scheduler
    "/api/scheduler/runs",
    "/api/timeline",
    # Models
    "/api/models",
    "/api/models-with-pricing",
    "/api/pricing-config",
    "/api/print-files",
    # Spools / Inventory
    "/api/spools",
    "/api/spools/low-stock",
    "/api/spools/export",
    "/api/filaments",
    "/api/filaments/combined",
    # Drying logs (nested under filaments router)
    "/api/drying-logs",
    # Orders
    "/api/orders",
    "/api/products",
    # Consumables
    "/api/consumables",
    "/api/consumables/low-stock",
    # Alerts / Notifications
    "/api/alerts",
    "/api/alerts/unread-count",
    "/api/alerts/summary",
    "/api/alert-preferences",
    "/api/smtp-config",
    "/api/push/vapid-key",
    "/api/webhooks",
    # Vision
    "/api/vision/detections",
    "/api/vision/settings",
    "/api/vision/stats",
    "/api/vision/models",
    "/api/vision/training-data",
    # Archives
    "/api/archives",
    "/api/archives/log",
    "/api/projects",
    "/api/tags",
    # Cameras / Timelapses
    "/api/cameras",
    "/api/timelapses",
    # Reporting / Analytics
    "/api/stats",
    "/api/analytics",
    "/api/analytics/failures",
    "/api/analytics/time-accuracy",
    "/api/reports/chargebacks",
    "/api/report-schedules",
    "/api/audit-logs",
    # Exports
    "/api/export/jobs",
    "/api/export/spools",
    "/api/export/filament-usage",
    "/api/export/models",
    "/api/export/audit-logs",
    # Organizations
    "/api/orgs",
    "/api/users",
    "/api/groups",
    "/api/permissions",
    # Sessions / Tokens
    "/api/sessions",
    "/api/tokens",
    "/api/quotas",
    # System / Admin
    "/api/health",
    "/api/license",
    "/api/license/installation-id",
    "/api/config",
    "/api/config/ip-allowlist",
    "/api/config/retention",
    "/api/config/quiet-hours",
    "/api/config/mqtt-republish",
    "/api/metrics",
    "/api/branding",
    "/api/settings/education-mode",
    "/api/settings/language",
    "/api/setup/status",
    # Maintenance
    "/api/maintenance/tasks",
    "/api/maintenance/logs",
    "/api/maintenance/status",
    # Slicer Profiles
    "/api/profiles",
    # Backups
    "/api/backups",
    # Admin
    "/api/admin/logs",
    "/api/admin/sessions",
    "/api/admin/quotas",
    # Bambu
    "/api/bambu/filament-types",
    # Search
    "/api/search",
    # Education usage reports
    "/api/education/usage-report",
    # Settings
    "/api/settings/energy-rate",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_headers():
    """Get admin auth headers (API key + JWT)."""
    if not ADMIN_PASSWORD:
        pytest.skip("ADMIN_PASSWORD not set — cannot authenticate for smoke tests")
    token = _login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        pytest.skip(f"Failed to login as {ADMIN_USERNAME}")
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", GET_ENDPOINTS)
def test_get_no_500(endpoint, admin_headers):
    """GET {endpoint} must not return a 500 error."""
    resp = requests.get(
        f"{BASE_URL}{endpoint}",
        headers=admin_headers,
        timeout=15,
    )
    assert resp.status_code < 500, (
        f"{endpoint} returned {resp.status_code}: {resp.text[:200]}"
    )
