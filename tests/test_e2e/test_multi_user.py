"""
O.D.I.N. Section 9 E2E: Multi-User Roles (S9.7)
=================================================
Admin sees user management, operator can manage jobs, operator blocked
from admin settings, viewer sees no mutation buttons, user creation API.

Run: ADMIN_PASSWORD=xxx pytest tests/test_e2e/test_multi_user.py -v --tb=short
"""

import os
import pytest

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")


class TestAdminUserManagement:
    """Admin should have full user management access."""

    def test_admin_sees_user_management(self, admin_page):
        """Admin Settings page should show user management section."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body").lower()
        has_users = (
            "user" in body and ("manage" in body or "create" in body or "add" in body)
        ) or "user management" in body
        # Also check for direct UI elements
        user_links = admin_page.locator(
            'a:has-text("Users"), button:has-text("Users"), '
            '[href*="users"], :text("User Management")'
        )
        assert has_users or user_links.count() > 0, \
            "Admin doesn't see user management in Settings"

    def test_admin_can_create_user_via_api(self, api):
        """Admin can create a user via API."""
        import uuid
        username = f"multiuser_test_{uuid.uuid4().hex[:6]}"
        r = api.post("/api/users", json={
            "username": username,
            "email": f"{username}@test.local",
            "password": os.environ["ADMIN_PASSWORD"],
            "role": "viewer",
        })
        if r.status_code in (403, 402):
            pytest.skip("User creation blocked — likely license limit")
        assert r.status_code in (200, 201), \
            f"Admin user creation failed: {r.status_code}"
        # Cleanup
        uid = r.json().get("id")
        if uid:
            api.delete(f"/api/users/{uid}")


class TestOperatorConstraints:
    """Operator can manage jobs but not admin-only features."""

    def test_operator_can_access_jobs(self, operator_page):
        """Operator should be able to view the jobs page."""
        operator_page.goto(f"{FRONTEND_URL}/jobs", wait_until="networkidle", timeout=15000)
        body = operator_page.inner_text("body")
        assert len(body.strip()) > 20, "Jobs page empty for operator"
        assert "forbidden" not in body.lower(), "Operator blocked from jobs page"

    def test_operator_blocked_from_settings(self, operator_page):
        """Operator should not see admin Settings tabs."""
        operator_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        body = operator_page.inner_text("body").lower()
        url = operator_page.url
        # Operator should either be redirected away or see limited content
        admin_keywords = ["smtp", "sso", "oidc", "branding", "backup"]
        found_admin = [k for k in admin_keywords if k in body]
        if "/settings" in url and len(found_admin) >= 2:
            pytest.fail(f"Operator sees admin settings: {found_admin}")

    def test_operator_cannot_see_user_management(self, operator_page):
        """Operator should not see user management links."""
        operator_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        user_mgmt = operator_page.locator(
            'a:has-text("Users"), a[href*="/users"], '
            ':text("User Management"), button:has-text("Add User")'
        )
        visible = sum(1 for i in range(user_mgmt.count()) if user_mgmt.nth(i).is_visible())
        assert visible == 0, f"Operator sees {visible} user management elements"


class TestViewerReadOnly:
    """Viewer should have read-only access with no mutation buttons."""

    def test_viewer_no_add_buttons_on_printers(self, viewer_page):
        """Viewer should not see Add/Delete/Edit buttons on printers page."""
        viewer_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        mutation_btns = viewer_page.locator(
            'button:has-text("Add"), button:has-text("Delete"), '
            'button:has-text("Edit"), button:has-text("Remove"), '
            'button:has-text("New")'
        )
        visible = sum(1 for i in range(mutation_btns.count()) if mutation_btns.nth(i).is_visible())
        assert visible == 0, \
            f"Viewer sees {visible} mutation button(s) on printers page"

    def test_viewer_no_add_buttons_on_jobs(self, viewer_page):
        """Viewer should not see job creation buttons."""
        viewer_page.goto(f"{FRONTEND_URL}/jobs", wait_until="networkidle", timeout=15000)
        # "New Job" is gated behind canDo('jobs.create') — should be hidden
        create_btns = viewer_page.locator(
            'button:has-text("Add Job"), button:has-text("Create Job"), '
            'button:has-text("New Job")'
        )
        visible_create = sum(1 for i in range(create_btns.count()) if create_btns.nth(i).is_visible())
        assert visible_create == 0, \
            f"Viewer sees {visible_create} job creation button(s)"
        # "Run Scheduler" is currently visible to all roles (known gap)
        scheduler_btn = viewer_page.locator('button:has-text("Run Scheduler")')
        if sum(1 for i in range(scheduler_btn.count()) if scheduler_btn.nth(i).is_visible()) > 0:
            pytest.xfail("Run Scheduler button visible to viewer — needs RBAC gate")
