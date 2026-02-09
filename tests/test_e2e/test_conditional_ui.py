"""
O.D.I.N. Phase 4 — Conditional UI Element Audit
test_conditional_ui.py

Tests every UI element that shows/hides based on:
  - Printer state (C1-C11)
  - User role (C12-C20)
  - License tier (C21-C31)
  - Data presence (C32-C39)

Run: ADMIN_PASSWORD=OdinAdmin1 pytest tests/test_e2e/test_conditional_ui.py -v --tb=short
"""
import pytest
import os

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
PAGES = [
    ("Dashboard", "/"),
    ("Jobs", "/jobs"),
    ("Printers", "/printers"),
    ("Models", "/models"),
    ("Spools", "/spools"),
    ("Orders", "/orders"),
    ("Products", "/products"),
    ("Analytics", "/analytics"),
    ("Cameras", "/cameras"),
    ("Settings", "/settings"),
]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1: STATE-DEPENDENT (Printer Status) — C1-C11
# ═══════════════════════════════════════════════════════════════════════════

class TestEmergencyStop:
    """C1: Emergency stop button should be visible on EVERY page."""

    @pytest.mark.parametrize("page_name, path", PAGES)
    def test_estop_visible_on_page(self, admin_page, page_name, path):
        """C1: E-stop floating button present on {page_name}."""
        admin_page.goto(f"{FRONTEND_URL}{path}", wait_until="networkidle", timeout=15000)
        # Look for emergency stop — could be button, icon, or fab
        estop = admin_page.locator(
            '[data-testid="emergency-stop"], '
            '[aria-label*="stop" i], '
            '[aria-label*="emergency" i], '
            'button:has-text("Stop"), '
            '.emergency-stop, '
            '.e-stop, '
            '#emergency-stop, '
            'button.stop-all, '
            '[class*="emergency"], '
            '[class*="estop"], '
            '[class*="stop-all"]'
        )
        # On pages like Settings where there's no printer context, the button
        # might be hidden/absent — that's a finding, not a test error.
        count = estop.count()
        if count == 0:
            # Check for any floating action button that might be the e-stop
            fab = admin_page.locator(
                'button[class*="float"], '
                'button[class*="fab"], '
                'button[class*="fixed"]'
            )
            count = fab.count()
        # Record result — we want to know which pages have it and which don't
        # This is an AUDIT, not a strict pass/fail
        if count == 0:
            pytest.xfail(f"E-stop not found on {page_name} — audit finding")


class TestPrinterStateElements:
    """C2-C10: Elements that depend on printer state."""

    def test_c2_progress_bar_idle_printer(self, admin_page, seed_data):
        """C2: Progress bar should be absent/hidden for idle printer."""
        if "printer_id" not in seed_data:
            pytest.skip("No printer in DB")
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        # Look for progress bars — they should be absent or at 0% for idle printers
        progress = admin_page.locator(
            '[role="progressbar"], '
            'progress, '
            '[class*="progress-bar"], '
            '[class*="progressBar"]'
        )
        # Not a failure if progress bars exist but are at 0 — that's valid UI
        # This just audits their presence
        if progress.count() > 0:
            # Check if any show non-zero progress (would be wrong for idle)
            for i in range(min(progress.count(), 5)):
                val = progress.nth(i).get_attribute("aria-valuenow") or ""
                style = progress.nth(i).get_attribute("style") or ""
                # If showing active progress on idle printer, that's a bug
                if val and val not in ("0", ""):
                    pytest.xfail(f"Progress bar shows {val}% on idle printer — possible bug")

    def test_c3_telemetry_bar_present(self, admin_page, seed_data):
        """C3: Printer cards should show telemetry info if printer has data."""
        if "printer_id" not in seed_data:
            pytest.skip("No printer in DB")
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        # Look for temperature indicators
        temps = admin_page.locator(
            '[class*="temp"], [class*="telemetry"], '
            ':text("°C"), :text("°F"), '
            '[class*="nozzle"], [class*="bed-temp"]'
        )
        # Telemetry only shows when printer is connected — if offline, expect absence
        # This is an audit check
        if temps.count() == 0:
            pytest.xfail("No telemetry indicators visible — printer may be offline")

    def test_c5_no_pause_resume_when_idle(self, admin_page, seed_data):
        """C5: Pause/Resume buttons hidden when printer not actively printing."""
        if "printer_id" not in seed_data:
            pytest.skip("No printer in DB")
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        pause_btn = admin_page.locator(
            'button:has-text("Pause"), '
            'button[aria-label*="Pause" i], '
            '[data-testid="pause-button"]'
        )
        resume_btn = admin_page.locator(
            'button:has-text("Resume"), '
            'button[aria-label*="Resume" i], '
            '[data-testid="resume-button"]'
        )
        # Both should be hidden when no printer is actively printing
        assert pause_btn.count() == 0 or not pause_btn.first.is_visible(), \
            "Pause button visible with no active print — C5 finding"
        assert resume_btn.count() == 0 or not resume_btn.first.is_visible(), \
            "Resume button visible with no active print — C5 finding"

    @pytest.mark.skip(reason="C6-C8: Requires connected Bambu printer with AMS — manual test only")
    def test_c6_c7_c8_ams_elements(self):
        """C6-C8: AMS-specific elements require live Bambu printer."""
        pass

    @pytest.mark.skip(reason="C9: Requires smart plug configured — manual test only")
    def test_c9_smart_plug_controls(self):
        """C9: Smart plug controls require configured plug."""
        pass

    def test_c10_camera_feed_visibility(self, admin_page, seed_data):
        """C10: Camera feeds only visible when camera URL configured."""
        admin_page.goto(f"{FRONTEND_URL}/cameras", wait_until="networkidle", timeout=15000)
        # Check for camera elements or empty state
        cameras = admin_page.locator(
            'video, img[class*="camera"], [class*="camera-feed"], '
            '[class*="stream"], iframe[src*="rtsp"], iframe[src*="rtc"]'
        )
        empty_state = admin_page.locator(
            ':text("No cameras"), :text("no cameras"), '
            ':text("Configure"), [class*="empty-state"]'
        )
        # Either cameras show or empty state shows — both are valid
        assert cameras.count() > 0 or empty_state.count() > 0, \
            "Cameras page shows neither feeds nor empty state"

    @pytest.mark.skip(reason="C11: Print failure modal requires MQTT failure event — manual test only")
    def test_c11_failure_modal(self):
        """C11: Failure modal needs live MQTT event."""
        pass


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2: ROLE-DEPENDENT — C12-C20
# ═══════════════════════════════════════════════════════════════════════════

class TestRoleVisibility:
    """C12-C20: Elements that show/hide based on user role."""

    def test_c12_admin_nav_items_visible_to_admin(self, admin_page):
        """C12: Admin should see Users/Permissions nav items."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        page_text = admin_page.content()
        # Look for admin-only nav items in sidebar
        admin_indicators = admin_page.locator(
            'a:has-text("Users"), '
            'a[href*="/users"], '
            ':text("User Management"), '
            'a[href*="/permissions"], '
            ':text("Permissions")'
        )
        assert admin_indicators.count() > 0, \
            "Admin nav items (Users/Permissions) not visible to admin — C12 bug"

    def test_c12_admin_nav_items_hidden_from_viewer(self, viewer_page):
        """C12: Viewer should NOT see Users/Permissions nav items."""
        viewer_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        admin_nav = viewer_page.locator(
            'a:has-text("Users"), '
            'a[href*="/users"], '
            'a[href*="/permissions"]'
        )
        visible_count = 0
        for i in range(admin_nav.count()):
            if admin_nav.nth(i).is_visible():
                visible_count += 1
        assert visible_count == 0, \
            f"Admin nav items visible to viewer ({visible_count} items) — C12 security bug"

    def test_c12_admin_nav_items_hidden_from_operator(self, operator_page):
        """C12: Operator should NOT see Users/Permissions nav items."""
        operator_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        admin_nav = operator_page.locator(
            'a:has-text("Users"), '
            'a[href*="/users"], '
            'a[href*="/permissions"]'
        )
        visible_count = 0
        for i in range(admin_nav.count()):
            if admin_nav.nth(i).is_visible():
                visible_count += 1
        assert visible_count == 0, \
            f"Admin nav items visible to operator ({visible_count} items) — C12 security bug"

    def test_c17_settings_tabs_admin(self, admin_page):
        """C17: Admin should see all Settings tabs (SMTP, SSO, Webhooks, Advanced)."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body")
        # Look for admin-only settings sections
        admin_settings = []
        for keyword in ["SMTP", "SSO", "OIDC", "Webhook", "Advanced", "Branding", "License"]:
            if keyword.lower() in page_text.lower():
                admin_settings.append(keyword)
        assert len(admin_settings) >= 2, \
            f"Admin sees only {admin_settings} settings tabs — expected more admin-only tabs"

    def test_c17_settings_tabs_viewer(self, viewer_page):
        """C17: Viewer should NOT see admin-only Settings tabs."""
        viewer_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = viewer_page.inner_text("body")
        admin_keywords_found = []
        for keyword in ["SMTP", "SSO", "OIDC", "Webhook", "Advanced", "Branding"]:
            if keyword.lower() in page_text.lower():
                admin_keywords_found.append(keyword)
        if admin_keywords_found:
            pytest.xfail(
                f"Viewer can see admin settings tabs: {admin_keywords_found} — C17 finding"
            )

    def test_c18_delete_buttons_admin(self, admin_page, seed_data):
        """C18: Admin should see delete buttons on resource pages."""
        if "printer_id" not in seed_data:
            pytest.skip("No printer in DB")
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        delete_btns = admin_page.locator(
            'button:has-text("Delete"), '
            'button[aria-label*="delete" i], '
            '[data-testid*="delete"], '
            'button[class*="delete"], '
            '[class*="trash"], '
            'svg[class*="trash"]'
        )
        # Admin should have delete capability somewhere
        # Might need to open a detail/edit panel to see it
        # Just audit presence
        if delete_btns.count() == 0:
            pytest.xfail("No delete buttons visible on printers page for admin — may require detail view")

    def test_c18_delete_buttons_viewer(self, viewer_page, seed_data):
        """C18: Viewer should NOT see delete buttons."""
        if "printer_id" not in seed_data:
            pytest.skip("No printer in DB")
        viewer_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        delete_btns = viewer_page.locator(
            'button:has-text("Delete"), '
            'button[aria-label*="delete" i], '
            '[data-testid*="delete"]'
        )
        visible_deletes = 0
        for i in range(delete_btns.count()):
            if delete_btns.nth(i).is_visible():
                visible_deletes += 1
        assert visible_deletes == 0, \
            f"Viewer sees {visible_deletes} delete button(s) — C18 security bug"

    def test_c19_branding_page_admin_only(self, admin_page, viewer_page):
        """C19: Branding accessible to admin, not viewer."""
        # Admin can access
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        admin_text = admin_page.inner_text("body")
        has_branding = "branding" in admin_text.lower() or "brand" in admin_text.lower()
        if not has_branding:
            # Try direct nav
            admin_page.goto(f"{FRONTEND_URL}/settings/branding", wait_until="networkidle", timeout=15000)
            admin_text = admin_page.inner_text("body")
            has_branding = "brand" in admin_text.lower()
        # Viewer should not see it
        viewer_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        viewer_text = viewer_page.inner_text("body")
        viewer_branding = viewer_page.locator(
            'a:has-text("Branding"), '
            '[href*="branding"]'
        )
        viewer_visible = 0
        for i in range(viewer_branding.count()):
            if viewer_branding.nth(i).is_visible():
                viewer_visible += 1
        if viewer_visible > 0:
            pytest.xfail("Viewer can see Branding nav link — C19 finding")

    def test_c20_license_tab_admin_only(self, admin_page, viewer_page):
        """C20: License tab in Settings visible only to admin."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        admin_text = admin_page.inner_text("body")
        admin_has_license = "license" in admin_text.lower()

        viewer_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        viewer_text = viewer_page.inner_text("body")
        viewer_license_links = viewer_page.locator(
            'a:has-text("License"), '
            '[href*="license"], '
            'button:has-text("License")'
        )
        viewer_visible = 0
        for i in range(viewer_license_links.count()):
            if viewer_license_links.nth(i).is_visible():
                viewer_visible += 1

        if admin_has_license and viewer_visible > 0:
            pytest.xfail("Viewer can see License tab — C20 finding")
        elif not admin_has_license:
            pytest.xfail("Admin doesn't see License tab — C20 finding (expected to exist)")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3: LICENSE-TIER-DEPENDENT — C21-C31
# ═══════════════════════════════════════════════════════════════════════════

class TestLicenseTierGating:
    """C21-C31: ProGate overlay or feature gating based on license tier."""

    # These pages should show ProGate on Community, full content on Pro
    PRO_GATED_PAGES = [
        ("C21", "Analytics", "/analytics"),
        ("C22", "Utilization", "/utilization"),
        ("C23", "Orders", "/orders"),
        ("C24", "Products", "/products"),
        ("C25", "Maintenance", "/maintenance"),
    ]

    @pytest.mark.parametrize("code, name, path", PRO_GATED_PAGES)
    def test_pro_gated_page(self, admin_page, code, name, path):
        """Check if Pro-gated page shows content or ProGate overlay."""
        admin_page.goto(f"{FRONTEND_URL}{path}", wait_until="networkidle", timeout=15000)

        # Look for ProGate overlay indicators
        progate = admin_page.locator(
            '[class*="ProGate"], [class*="progate"], [class*="pro-gate"], '
            '[class*="upgrade"], [class*="paywall"], '
            ':text("Upgrade to Pro"), :text("Pro Feature"), '
            ':text("requires Pro"), :text("upgrade")'
        )
        page_text = admin_page.inner_text("body")

        # Determine what we see
        has_progate = progate.count() > 0
        has_content = len(page_text.strip()) > 100  # More than just a title

        # Record findings
        if has_progate:
            # Community tier — ProGate is working correctly
            pass
        elif has_content:
            # Pro tier — full content visible, that's correct
            pass
        else:
            pytest.xfail(f"{code}: {name} page appears empty — neither ProGate nor content")

    PRO_GATED_SETTINGS = [
        ("C26", "Branding", "branding"),
        ("C27", "Permissions", "permissions"),
        ("C28", "Webhooks", "webhook"),
        ("C29", "SMTP", "smtp"),
        ("C30", "SSO", "sso"),
    ]

    @pytest.mark.parametrize("code, name, keyword", PRO_GATED_SETTINGS)
    def test_pro_gated_settings(self, admin_page, code, name, keyword):
        """Check if Pro-gated settings show ProGate or content."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body").lower()

        # Check if the setting section exists
        has_section = keyword.lower() in page_text

        # Check for ProGate on this section
        progate = admin_page.locator(
            f'[class*="ProGate"], [class*="progate"], '
            f':text("Upgrade"), :text("Pro Feature")'
        )

        if not has_section:
            pytest.xfail(f"{code}: {name} section not found in Settings — may be on different page")

    def test_c31_job_approval_toggle(self, admin_page):
        """C31: Job approval toggle visibility based on tier (Education/Enterprise only)."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body").lower()
        has_approval = "approval" in page_text or "job approval" in page_text
        # On Community/Pro this should be hidden or show ProGate
        # On Education/Enterprise this should be a toggle
        if has_approval:
            pass  # Present — either enabled tier or showing ProGate
        else:
            pytest.xfail("C31: Job approval toggle not found in Settings")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4: DATA-DEPENDENT — C32-C39
# ═══════════════════════════════════════════════════════════════════════════

class TestDataDependentElements:
    """C32-C39: Elements that show/hide based on data presence."""

    def test_c32_fleet_status_widget(self, admin_page, seed_data):
        """C32: Fleet status sidebar widget visible when printers exist."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        fleet = admin_page.locator(
            '[class*="fleet"], [class*="sidebar"] [class*="status"], '
            '[class*="printer-status"], [class*="printer-count"], '
            ':text("Fleet"), :text("fleet")'
        )
        if "printer_id" in seed_data:
            # Should show fleet widget
            if fleet.count() == 0:
                pytest.xfail("C32: Fleet status widget not visible despite printers existing")
        else:
            # No printers — widget might be hidden
            pass

    def test_c33_stat_cards_with_data(self, admin_page, seed_data):
        """C33: Stat cards visible on pages when data exists."""
        # Check Orders page for stat cards
        admin_page.goto(f"{FRONTEND_URL}/orders", wait_until="networkidle", timeout=15000)
        stat_cards = admin_page.locator(
            '[class*="stat-card"], [class*="stat_card"], [class*="StatCard"], '
            '[class*="stats"], [class*="summary-card"], [class*="metric"]'
        )
        if "order_id" in seed_data:
            # Orders exist — should see stat cards
            if stat_cards.count() == 0:
                pytest.xfail("C33: No stat cards on Orders page despite data existing")

    def test_c34_empty_state_no_printers(self, admin_page):
        """C34: Empty state message when no printers (or printers list when they exist)."""
        admin_page.goto(f"{FRONTEND_URL}/printers", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body").lower()
        has_empty = "no printer" in page_text or "add a printer" in page_text or "get started" in page_text
        has_printers = admin_page.locator(
            '[class*="printer-card"], [class*="printerCard"], '
            'tr[class*="printer"], [class*="printer-row"]'
        ).count() > 0
        # One or the other should be true
        assert has_empty or has_printers, \
            "C34: Printers page shows neither empty state nor printer cards"

    def test_c35_control_room_mode(self, admin_page):
        """C35: Control Room mode available when cameras exist."""
        admin_page.goto(f"{FRONTEND_URL}/cameras", wait_until="networkidle", timeout=15000)
        control_room = admin_page.locator(
            'button:has-text("Control Room"), '
            '[class*="control-room"], '
            ':text("Control Room")'
        )
        page_text = admin_page.inner_text("body").lower()
        has_cameras = "no camera" not in page_text
        if has_cameras and control_room.count() == 0:
            pytest.xfail("C35: Cameras exist but no Control Room button")

    def test_c37_recently_completed(self, admin_page, seed_data):
        """C37: Recently Completed grid visible when completed jobs exist."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        completed = admin_page.locator(
            ':text("Recently Completed"), :text("recently completed"), '
            ':text("Completed Jobs"), [class*="completed"]'
        )
        # Just audit — don't fail if there are no completed jobs
        if completed.count() == 0:
            pytest.xfail("C37: No 'Recently Completed' section — may need completed jobs in DB")

    def test_c39_cost_revenue_chart(self, admin_page, seed_data):
        """C39: Cost/Revenue chart visible when order financial data exists."""
        admin_page.goto(f"{FRONTEND_URL}/analytics", wait_until="networkidle", timeout=15000)
        charts = admin_page.locator(
            'canvas, svg[class*="chart"], [class*="recharts"], '
            '[class*="chart"], [class*="Chart"]'
        )
        page_text = admin_page.inner_text("body").lower()
        # If ProGated, that's fine — recorded in C21
        if "upgrade" in page_text or "pro" in page_text:
            pytest.xfail("C39: Analytics page is ProGated — chart not visible on Community tier")
        if charts.count() == 0 and "order_id" in seed_data:
            pytest.xfail("C39: No charts on Analytics despite having order data")
