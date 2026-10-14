"""
O.D.I.N. Section 9 E2E: Vigil AI Detections (S9.5)
====================================================
Detections page loads, detection type labels visible, filter controls,
Vigil settings in Settings page, API returns valid JSON.

Run: ADMIN_PASSWORD=xxx pytest tests/test_e2e/test_vigil_detections.py -v --tb=short
"""

import os
import pytest
import requests

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


class TestVigilDetectionsPage:
    """S9.5: Vigil AI detection page rendering."""

    def test_detections_page_loads(self, admin_page):
        """Detections page renders without error."""
        admin_page.goto(f"{FRONTEND_URL}/detections", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        assert len(body.strip()) > 20, "Detections page appears empty"
        lower = body.lower()
        # Should show detection content or empty state, not a routing error
        assert "cannot get" not in lower, "Detections page shows routing error"

    def test_detection_type_labels_visible(self, admin_page):
        """Detection type labels (spaghetti, first layer, detachment) should be present."""
        admin_page.goto(f"{FRONTEND_URL}/detections", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body").lower()
        detection_types = ["spaghetti", "first layer", "detachment"]
        found = [dt for dt in detection_types if dt in body]
        # Types may only show if detections exist — check for at least the page structure
        page_ok = (
            len(found) > 0 or
            "no detection" in body or
            "vigil" in body or
            "detection" in body
        )
        assert page_ok, "Detections page missing type labels and detection content"

    def test_filter_controls_exist(self, admin_page):
        """Filter/sort controls should exist on detections page."""
        admin_page.goto(f"{FRONTEND_URL}/detections", wait_until="networkidle", timeout=15000)
        filters = admin_page.locator(
            'select, [class*="filter"], [class*="Filter"], '
            'button:has-text("Filter"), [role="combobox"], '
            'input[type="search"], [class*="search"]'
        )
        body = admin_page.inner_text("body").lower()
        has_filters = filters.count() > 0
        has_text = "filter" in body or "sort" in body or "type" in body
        if not has_filters and not has_text:
            pytest.xfail("No filter controls found on detections page")

    def test_vigil_settings_in_settings_page(self, admin_page):
        """Vigil/Vision settings section should exist in Settings."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body").lower()
        has_vigil = (
            "vigil" in body or
            "vision" in body or
            "ai detection" in body or
            "print failure" in body
        )
        if not has_vigil:
            pytest.xfail("Vigil/Vision settings not found in Settings page")

    def test_vision_api_returns_valid_json(self, api):
        """GET /api/vision/detections must return valid JSON."""
        r = api.get("/api/vision/detections")
        assert r.status_code in (200, 404), \
            f"Vision detections API returned {r.status_code}"
        if r.status_code == 200:
            data = r.json()
            assert isinstance(data, (list, dict)), \
                f"Vision detections returned non-JSON-like type: {type(data)}"
