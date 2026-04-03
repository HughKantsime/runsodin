"""
O.D.I.N. Section 9 E2E: License Activation (S9.6)
===================================================
License section in Settings, tier display, upload UI, API tier response.

Run: ADMIN_PASSWORD=xxx pytest tests/test_e2e/test_license_upgrade.py -v --tb=short
"""

import os
import pytest

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")


class TestLicenseUI:
    """S9.6: License activation and tier display in UI."""

    def _go_to_system_tab(self, page):
        """Navigate to Settings > System tab where license section lives."""
        page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        system_tab = page.locator(
            'button:has-text("System"), a:has-text("System"), '
            '[data-tab="system"]'
        )
        if system_tab.count() > 0:
            system_tab.first.click()
            page.wait_for_timeout(500)

    def test_license_section_in_settings(self, admin_page):
        """License section should exist in Settings page for admin."""
        self._go_to_system_tab(admin_page)
        body = admin_page.inner_text("body").lower()
        has_license = "license" in body or "current license" in body
        assert has_license, "License section not found in Settings > System tab"

    def test_tier_name_displayed(self, admin_page):
        """Current license tier name should be displayed."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body").lower()
        tiers = ["community", "pro", "education", "enterprise"]
        found_tier = any(t in body for t in tiers)
        if not found_tier:
            # May need to navigate to license subsection
            license_link = admin_page.locator(
                'a:has-text("License"), button:has-text("License"), '
                '[href*="license"]'
            )
            if license_link.count() > 0:
                license_link.first.click()
                admin_page.wait_for_timeout(500)
                body = admin_page.inner_text("body").lower()
                found_tier = any(t in body for t in tiers)
        assert found_tier, "No tier name (Community/Pro/Education/Enterprise) displayed"

    def test_license_upload_ui_exists(self, admin_page):
        """License upload mechanism should exist (button, drop zone, or file input)."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        # Navigate to license section if needed
        license_link = admin_page.locator(
            'a:has-text("License"), button:has-text("License"), '
            '[href*="license"]'
        )
        if license_link.count() > 0:
            license_link.first.click()
            admin_page.wait_for_timeout(500)

        upload_elements = admin_page.locator(
            'button:has-text("Upload"), button:has-text("Activate"), '
            'button:has-text("Apply"), input[type="file"], '
            'textarea[placeholder*="license" i], '
            '[class*="upload"], [class*="dropzone"]'
        )
        body = admin_page.inner_text("body").lower()
        has_upload = upload_elements.count() > 0 or "upload" in body or "activate" in body
        assert has_upload, "No license upload mechanism found"

    def test_license_api_returns_tier(self, api):
        """GET /api/license must return current tier."""
        r = api.get("/api/license")
        assert r.status_code == 200, f"License API returned {r.status_code}"
        data = r.json()
        license_data = data.get("license", data)
        tier = license_data.get("tier", license_data.get("plan"))
        assert tier is not None, f"License API missing tier field: {list(license_data.keys())}"
        assert tier.lower() in {"community", "pro", "education", "enterprise"}, \
            f"Unknown tier: {tier}"
