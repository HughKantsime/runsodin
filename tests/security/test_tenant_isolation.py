"""
Layer 3 Security: Tenant Isolation (IDOR) Tests
================================================
Tests that lower-privilege users cannot access or modify resources
belonging to other users or higher-privilege roles.

Run: pytest tests/security/test_tenant_isolation.py -v --tb=short
"""

import pytest
import requests

from .conftest import BASE_URL, _headers, TEST_DUMMY_PASSWORD


class TestOperatorCannotAccessAdminRoutes:
    """Operator role must be blocked from admin-only endpoints."""

    ADMIN_ONLY_ENDPOINTS = [
        ("GET", "/api/users"),
        ("GET", "/api/backups"),
        ("GET", "/api/admin/oidc"),
        ("GET", "/api/admin/sessions"),
    ]

    @pytest.mark.parametrize("method,endpoint", ADMIN_ONLY_ENDPOINTS,
                             ids=[e[1] for e in ADMIN_ONLY_ENDPOINTS])
    def test_operator_blocked_from_admin_route(self, operator_token, method, endpoint):
        """Operator hitting admin-only route must get 403."""
        r = requests.request(
            method,
            f"{BASE_URL}{endpoint}",
            headers=_headers(operator_token),
            timeout=10,
        )
        assert r.status_code == 403, \
            f"Operator accessed {endpoint}! Got {r.status_code} — IDOR vulnerability"


class TestViewerCannotMutate:
    """Viewer role must be blocked from mutation endpoints."""

    def test_viewer_cannot_create_user(self, viewer_token):
        """Viewer creating a user must get 403."""
        r = requests.post(
            f"{BASE_URL}/api/users",
            json={
                "username": "idor_test_viewer_create",
                "email": "idor@test.local",
                "password": TEST_DUMMY_PASSWORD,
                "role": "viewer",
            },
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert r.status_code == 403, \
            f"Viewer created a user! Got {r.status_code}"

    def test_viewer_cannot_delete_printer(self, viewer_token):
        """Viewer deleting a printer must get 403."""
        r = requests.delete(
            f"{BASE_URL}/api/printers/1",
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert r.status_code in (403, 404), \
            f"Viewer deleted printer! Got {r.status_code}"

    def test_viewer_cannot_create_backup(self, viewer_token):
        """Viewer creating a backup must get 403."""
        r = requests.post(
            f"{BASE_URL}/api/backups",
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert r.status_code == 403, \
            f"Viewer created backup! Got {r.status_code}"

    def test_viewer_cannot_modify_oidc(self, viewer_token):
        """Viewer modifying OIDC config must get 403."""
        r = requests.put(
            f"{BASE_URL}/api/admin/oidc",
            json={"display_name": "Hacked SSO"},
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert r.status_code == 403, \
            f"Viewer modified OIDC config! Got {r.status_code}"


class TestCrossUserDataAccess:
    """Users must not access other users' private data."""

    def test_viewer_cannot_export_other_user(self, viewer_token, admin_token):
        """Viewer exporting another user's data must get 403 or 404."""
        # Get admin user ID
        r = requests.get(
            f"{BASE_URL}/api/users",
            headers=_headers(admin_token),
            timeout=10,
        )
        if r.status_code != 200:
            pytest.skip("Cannot list users")
        users = r.json()
        admin_user = next((u for u in users if u.get("role") == "admin"), None)
        if not admin_user:
            pytest.skip("No admin user found in list")

        # Viewer tries to export admin's data
        export_r = requests.get(
            f"{BASE_URL}/api/users/{admin_user['id']}/export",
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert export_r.status_code in (403, 404), \
            f"Viewer exported admin data! Got {export_r.status_code}"

    def test_viewer_cannot_admin_revoke_session(self, viewer_token):
        """Viewer using admin session revoke endpoint must get 403."""
        r = requests.delete(
            f"{BASE_URL}/api/admin/sessions/99999",
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert r.status_code in (403, 404), \
            f"Viewer used admin session revoke! Got {r.status_code}"
