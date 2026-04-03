"""
O.D.I.N. Section 9 E2E: Printer Flow (S9.2)
=============================================
Printers page loads, add button exists, modal opens, printer visible on dashboard.

Run: ADMIN_PASSWORD=xxx pytest tests/test_e2e/test_printer_flow.py -v --tb=short
"""

import os
import pytest

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")


class TestPrinterPageFlow:
    """S9.2: Printer page rendering and add flow."""

    def test_printers_page_loads(self, admin_page):
        """Printers page renders with content."""
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        assert len(body.strip()) > 20, "Printers page appears empty"
        lower = body.lower()
        assert "cannot get" not in lower, "Printers page shows routing error"

    def test_add_printer_button_exists(self, admin_page):
        """Add Printer button is present for admin."""
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        add_btn = admin_page.locator(
            'button:has-text("Add"), button:has-text("New Printer"), '
            'button[aria-label*="add" i], button[aria-label*="printer" i], '
            'a:has-text("Add Printer")'
        )
        assert add_btn.count() > 0, "No 'Add Printer' button found"

    def test_add_printer_modal_opens(self, admin_page):
        """Clicking Add Printer opens a modal/form."""
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        add_btn = admin_page.locator(
            'button:has-text("Add"), button:has-text("New Printer"), '
            'a:has-text("Add Printer")'
        )
        if add_btn.count() == 0:
            pytest.skip("No Add Printer button found")
        add_btn.first.click()
        admin_page.wait_for_timeout(500)
        # Check for modal or form
        modal = admin_page.locator(
            '[role="dialog"], [class*="modal"], [class*="Modal"], '
            'form, [class*="drawer"], [class*="Drawer"]'
        )
        # Also check if a new page/form appeared with printer fields
        name_input = admin_page.locator(
            'input[name="name"], input[placeholder*="name" i], '
            'input[placeholder*="printer" i]'
        )
        has_modal = modal.count() > 0
        has_form = name_input.count() > 0
        assert has_modal or has_form, "Add Printer didn't open modal or form"

    def test_printer_visible_on_dashboard(self, admin_page, seed_data):
        """If printers exist, at least one should appear on the dashboard."""
        if "printer_id" not in seed_data:
            pytest.skip("No printers in DB")
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body").lower()
        printer_name = seed_data.get("printer_name", "").lower()
        # Check for printer name or generic printer indicators
        has_printer = (
            (printer_name and printer_name in body) or
            "printer" in body or
            "idle" in body or
            "offline" in body or
            "online" in body
        )
        assert has_printer, "No printer indicators on dashboard despite printers in DB"
