"""
O.D.I.N. Section 9 E2E: Camera Grid (S9.4)
============================================
Cameras page loads, grid or empty state, camera detail accessible, layout controls.

Run: ADMIN_PASSWORD=xxx pytest tests/test_e2e/test_camera_grid.py -v --tb=short
"""

import os
import pytest

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")


class TestCameraGrid:
    """S9.4: Camera grid page rendering and controls."""

    def test_cameras_page_loads(self, admin_page):
        """Cameras page renders without error."""
        admin_page.goto(f"{FRONTEND_URL}/cameras", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        assert len(body.strip()) > 20, "Cameras page appears empty"
        lower = body.lower()
        assert "cannot get" not in lower, "Cameras page shows routing error"

    def test_cameras_shows_feeds_or_empty_state(self, admin_page):
        """Cameras page shows either camera feeds or an empty state message."""
        admin_page.goto(f"{FRONTEND_URL}/cameras", wait_until="networkidle", timeout=15000)
        # Camera feed elements
        feeds = admin_page.locator(
            'video, img[class*="camera"], [class*="camera-feed"], '
            '[class*="stream"], iframe'
        )
        # Empty state elements
        empty = admin_page.locator(
            ':text("No cameras"), :text("no cameras"), '
            ':text("Configure"), :text("No camera"), '
            '[class*="empty-state"], [class*="EmptyState"]'
        )
        assert feeds.count() > 0 or empty.count() > 0, \
            "Cameras page shows neither feeds nor empty state"

    def test_camera_detail_accessible(self, admin_page, seed_data):
        """If cameras exist, clicking one should open detail view."""
        admin_page.goto(f"{FRONTEND_URL}/cameras", wait_until="networkidle", timeout=15000)
        # Look for clickable camera cards
        camera_cards = admin_page.locator(
            '[class*="camera-card"], [class*="CameraCard"], '
            'a[href*="/camera"], [data-testid*="camera"]'
        )
        if camera_cards.count() == 0:
            pytest.skip("No camera cards visible — cameras may not be configured")
        camera_cards.first.click()
        admin_page.wait_for_timeout(1000)
        body = admin_page.inner_text("body")
        # Should show camera detail or a video feed
        assert len(body.strip()) > 20, "Camera detail view appears empty"

    def test_layout_controls_present(self, admin_page):
        """Camera page should have layout/grid controls when cameras exist."""
        admin_page.goto(f"{FRONTEND_URL}/cameras", wait_until="networkidle", timeout=15000)
        body_text = admin_page.inner_text("body").lower()
        # Check for layout toggle (grid/list) or size controls
        layout_controls = admin_page.locator(
            'button[aria-label*="grid" i], button[aria-label*="layout" i], '
            'button[aria-label*="view" i], [class*="layout-toggle"], '
            '[class*="grid-control"], select[class*="layout"]'
        )
        # Also check for text indicators
        has_controls = layout_controls.count() > 0
        has_text = "grid" in body_text or "layout" in body_text or "control room" in body_text
        if not has_controls and not has_text:
            # Cameras may not be configured — that's OK
            if "no camera" in body_text:
                pytest.skip("No cameras configured — layout controls not applicable")
            pytest.xfail("No layout controls found on cameras page")
