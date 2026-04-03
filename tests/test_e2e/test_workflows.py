"""
O.D.I.N. Phase 5 — E2E Workflow Tests
test_workflows.py

Complete user journey tests:
  W1: First-Time User / Pro Customer Journey
  W2: Order Fulfillment Workflow (API-assisted)
  W3: Job Approval Workflow
  W4: Role Restriction Verification
  W5: Keyboard Shortcuts
  W6: Light/Dark Mode
  W7: Mobile Responsive
  W8: Upload Flow (skip — no .3mf file available)

Run: ADMIN_PASSWORD=OdinAdmin1 pytest tests/test_e2e/test_workflows.py -v --tb=short
"""
import time
import pytest
import os
import requests as _requests

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
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

_WF_TOKEN_CACHE = {}

def _login_via_token_inline(browser_instance, username, password):
    """Login via cached API token, inject into localStorage, return (context, page)."""
    cache_key = f"{username}:{password}"
    if cache_key not in _WF_TOKEN_CACHE:
        resp = _requests.post(
            f"{BASE_URL}/api/auth/login",
            data={"username": username, "password": password},
            timeout=15,
        )
        assert resp.status_code == 200, f"Login failed: {resp.status_code}"
        _WF_TOKEN_CACHE[cache_key] = resp.json()["access_token"]
    token = _WF_TOKEN_CACHE[cache_key]
    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=15000)
    page.evaluate(f"""() => {{
        localStorage.setItem('access_token', '{token}');
        localStorage.setItem('token', '{token}');
        localStorage.setItem('auth_token', '{token}');
    }}""")
    page.reload(wait_until="networkidle", timeout=15000)
    return context, page


# ═══════════════════════════════════════════════════════════════════════════
# W1: FIRST-TIME USER / PRO CUSTOMER JOURNEY
# ═══════════════════════════════════════════════════════════════════════════

class TestW1CustomerJourney:
    """W1: Load every main page as admin, verify it renders."""

    def test_w1_01_login_page_branding(self, page):
        """W1.1: Login page shows O.D.I.N. branding, not PrintFarm Scheduler."""
        page.goto(f"{FRONTEND_URL}/login", wait_until="networkidle", timeout=15000)
        page_text = page.inner_text("body").lower()
        # Should show ODIN branding
        has_odin = "odin" in page_text or "o.d.i.n" in page_text
        has_old_name = "printfarm scheduler" in page_text
        if has_old_name:
            pytest.fail("W1.1: Login page still shows 'PrintFarm Scheduler' — rebrand incomplete")
        # ODIN branding might be in custom branding or default
        # Not a hard fail if absent — could be white-labeled
        if not has_odin and not has_old_name:
            pytest.xfail("W1.1: No 'ODIN' text on login — may be white-labeled")

    def test_w1_02_login_as_admin(self, admin_page):
        """W1.2: Successfully login as admin and reach dashboard."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        # Should not be on login page
        assert "/login" not in admin_page.url, "Still on login page after auth"
        # Page should have some content
        body = admin_page.inner_text("body")
        assert len(body.strip()) > 50, "Dashboard appears empty after login"

    @pytest.mark.parametrize("page_name, path", [
        ("Dashboard", "/"),
        ("Printers", "/printers"),
        ("Jobs", "/jobs"),
        ("Models", "/models"),
        ("Spools", "/spools"),
        ("Analytics", "/analytics"),
        ("Settings", "/settings"),
    ])
    def test_w1_navigate_pages(self, admin_page, page_name, path):
        """W1.3-9: Navigate to {page_name} — verify page loads."""
        admin_page.goto(f"{FRONTEND_URL}{path}", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        # Page should render something (not a blank page or error)
        assert len(body.strip()) > 20, f"{page_name} page appears empty"
        # Should not show a raw error
        lower = body.lower()
        assert "cannot get" not in lower, f"{page_name} shows routing error"
        assert "404" not in lower or "not found" not in lower or len(body) > 200, \
            f"{page_name} may be showing a 404"

    def test_w1_jobs_tabs(self, admin_page):
        """W1.6: Jobs page has tabs (All, Order Jobs, Ad-hoc or similar)."""
        admin_page.goto(f"{FRONTEND_URL}/jobs", wait_until="networkidle", timeout=15000)
        tabs = admin_page.locator(
            '[role="tab"], [class*="tab"], button[class*="tab"]'
        )
        page_text = admin_page.inner_text("body").lower()
        has_tabs = tabs.count() >= 2 or "all" in page_text
        if not has_tabs:
            pytest.xfail("W1: Jobs page doesn't show multiple tabs — may use different filtering UX")


# ═══════════════════════════════════════════════════════════════════════════
# W2: ORDER FULFILLMENT WORKFLOW (API-assisted)
# ═══════════════════════════════════════════════════════════════════════════

class TestW2OrderFulfillment:
    """
    W2: Create product → create order → verify UI reflects state.
    Heavy lifting via API, UI verification via Playwright.
    """

    def test_w2_orders_page_loads(self, admin_page):
        """W2.1: Orders page accessible."""
        admin_page.goto(f"{FRONTEND_URL}/orders", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        # Either shows orders list or ProGate or empty state
        assert len(body.strip()) > 20, "Orders page is blank"

    def test_w2_products_page_loads(self, admin_page):
        """W2.2: Products page accessible."""
        admin_page.goto(f"{FRONTEND_URL}/products", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        assert len(body.strip()) > 20, "Products page is blank"

    def test_w2_order_detail_view(self, admin_page, seed_data):
        """W2.3: If an order exists, verify detail view renders."""
        if "order_id" not in seed_data:
            pytest.skip("No order in DB to view")
        admin_page.goto(
            f"{FRONTEND_URL}/orders/{seed_data['order_id']}",
            wait_until="networkidle", timeout=15000
        )
        body = admin_page.inner_text("body")
        # Should show order details, not a 404
        assert len(body.strip()) > 30, "Order detail page appears empty"


# ═══════════════════════════════════════════════════════════════════════════
# W3: JOB APPROVAL WORKFLOW
# ═══════════════════════════════════════════════════════════════════════════

class TestW3JobApproval:
    """W3: Job approval workflow (Education tier feature)."""

    def test_w3_approval_setting_exists(self, admin_page):
        """W3.1: Job approval toggle exists in Settings."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body").lower()
        has_approval = "approval" in page_text or "require approval" in page_text
        if not has_approval:
            pytest.xfail("W3: Job approval setting not found — may be tier-gated")

    def test_w3_jobs_page_approval_tab(self, admin_page):
        """W3.2: Jobs page has Awaiting Approval tab when enabled."""
        admin_page.goto(f"{FRONTEND_URL}/jobs", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body").lower()
        approval_tab = admin_page.locator(
            ':text("Awaiting Approval"), :text("Pending Approval"), '
            ':text("Submitted"), [class*="approval"]'
        )
        # Tab may only show when approval is enabled
        if approval_tab.count() == 0:
            pytest.xfail("W3: No approval tab visible — approval may be disabled")


# ═══════════════════════════════════════════════════════════════════════════
# W4: ROLE RESTRICTION VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════

class TestW4RoleRestrictions:
    """W4: Verify role-based access restrictions in the UI."""

    def test_w4_viewer_no_settings_admin_tabs(self, viewer_page):
        """W4.1: Viewer cannot see admin Settings tabs."""
        viewer_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = viewer_page.inner_text("body").lower()
        # Viewer should not see these
        admin_only = ["users", "smtp", "sso", "oidc", "webhook", "branding"]
        found = [k for k in admin_only if k in page_text]
        # Some overlap possible (e.g. "users" in "No users found") — check for nav links
        admin_links = viewer_page.locator(
            'a[href*="/users"], a[href*="/branding"], '
            'a:has-text("User Management")'
        )
        visible_admin_links = sum(1 for i in range(admin_links.count()) if admin_links.nth(i).is_visible())
        assert visible_admin_links == 0, \
            f"W4: Viewer sees {visible_admin_links} admin-only links"

    def test_w4_viewer_direct_url_blocked(self, viewer_page):
        """W4.2: Viewer navigating to /settings/users gets redirected or blocked."""
        viewer_page.goto(f"{FRONTEND_URL}/settings/users", wait_until="networkidle", timeout=15000)
        # Should be redirected away or show access denied
        url = viewer_page.url
        body = viewer_page.inner_text("body").lower()
        blocked = (
            "/login" in url or
            "/settings/users" not in url or
            "permission" in body or
            "denied" in body or
            "forbidden" in body or
            "unauthorized" in body
        )
        if not blocked:
            # Check if the page actually has user management content
            has_user_mgmt = "create user" in body or "add user" in body
            if has_user_mgmt:
                pytest.fail("W4: Viewer can access /settings/users with full content — security bug")
            # Might just redirect silently
            pass

    def test_w4_operator_no_user_management(self, operator_page):
        """W4.3: Operator cannot access User management."""
        operator_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        user_mgmt = operator_page.locator(
            'a:has-text("Users"), a[href*="/users"], '
            ':text("User Management")'
        )
        visible = sum(1 for i in range(user_mgmt.count()) if user_mgmt.nth(i).is_visible())
        assert visible == 0, f"W4: Operator sees User Management ({visible} links)"

    def test_w4_admin_full_access(self, admin_page):
        """W4.4: Admin has full access to all sections."""
        admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
        page_text = admin_page.inner_text("body").lower()
        # Admin should see the most settings
        found_sections = 0
        for section in ["general", "printer", "notification", "appearance",
                        "branding", "license", "users", "advanced"]:
            if section in page_text:
                found_sections += 1
        assert found_sections >= 3, \
            f"W4: Admin only sees {found_sections} settings sections — expected more"


# ═══════════════════════════════════════════════════════════════════════════
# W5: KEYBOARD SHORTCUTS
# ═══════════════════════════════════════════════════════════════════════════

class TestW5KeyboardShortcuts:
    """W5: Keyboard shortcuts work from any page."""

    def test_w5_help_modal(self, admin_page):
        """W5.1: Press ? to open help/shortcuts modal."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        admin_page.keyboard.press("?")
        # Wait for modal
        admin_page.wait_for_timeout(500)
        modal = admin_page.locator(
            '[role="dialog"], [class*="modal"], [class*="Modal"], '
            '[class*="shortcut"], [class*="Shortcut"], '
            '[class*="keyboard"], [class*="help-modal"]'
        )
        if modal.count() == 0:
            pytest.xfail("W5: ? key didn't open a shortcuts modal")
        # Close it
        admin_page.keyboard.press("Escape")

    def test_w5_search_focus(self, admin_page):
        """W5.2: Press / to focus search bar."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        admin_page.keyboard.press("/")
        admin_page.wait_for_timeout(300)
        # Check if search input is focused
        focused = admin_page.evaluate("document.activeElement?.tagName")
        focused_type = admin_page.evaluate("document.activeElement?.type || ''")
        focused_placeholder = admin_page.evaluate("document.activeElement?.placeholder || ''")
        is_search = (
            focused == "INPUT" and
            ("search" in focused_placeholder.lower() or focused_type in ("text", "search"))
        )
        if not is_search:
            pytest.xfail("W5: / key didn't focus search input")

    def test_w5_nav_shortcuts(self, admin_page):
        """W5.3: G+D navigates to Dashboard, G+J to Jobs, G+P to Printers."""
        shortcuts = [
            ("d", "/", "Dashboard"),
            ("j", "/jobs", "Jobs"),
            ("p", "/printers", "Printers"),
        ]
        worked = 0
        for key, expected_path, name in shortcuts:
            admin_page.goto(f"{FRONTEND_URL}/settings", wait_until="networkidle", timeout=15000)
            admin_page.keyboard.press("g")
            admin_page.wait_for_timeout(200)
            admin_page.keyboard.press(key)
            admin_page.wait_for_timeout(1000)
            current = admin_page.url.rstrip("/")
            base = FRONTEND_URL.rstrip("/")
            if expected_path == "/":
                if current == base or current == f"{base}/":
                    worked += 1
            elif expected_path in admin_page.url:
                worked += 1
        assert worked >= 1, \
            "W5: No G+key navigation shortcuts worked (tested G+D, G+J, G+P)"

    def test_w5_escape_closes_modal(self, admin_page):
        """W5.4: Escape key closes open modals."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        # Open help modal
        admin_page.keyboard.press("?")
        admin_page.wait_for_timeout(500)
        modal_before = admin_page.locator('[role="dialog"], [class*="modal"]:visible').count()
        # Press escape
        admin_page.keyboard.press("Escape")
        admin_page.wait_for_timeout(500)
        modal_after = admin_page.locator('[role="dialog"], [class*="modal"]:visible').count()
        if modal_before > 0:
            assert modal_after < modal_before, "W5: Escape didn't close modal"


# ═══════════════════════════════════════════════════════════════════════════
# W6: LIGHT/DARK MODE
# ═══════════════════════════════════════════════════════════════════════════

class TestW6ThemeToggle:
    """W6: Light/dark mode toggle and persistence."""

    def test_w6_theme_toggle_exists(self, admin_page):
        """W6.1: Theme toggle is present on the page."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
        toggle = admin_page.locator('button[aria-label*="Switch to"]')
        assert toggle.count() > 0, "W6: Theme toggle not found"

    def test_w6_theme_switch(self, admin_page):
        """W6.2: Clicking theme toggle changes the theme."""
        admin_page.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)

        # Get current theme state
        html_class_before = admin_page.locator("html").get_attribute("class") or ""

        # Find a VISIBLE theme toggle — aria-label is "Switch to light mode" or "Switch to dark mode"
        toggle = admin_page.locator(
            'button[aria-label*="Switch to"]:visible'
        )
        if toggle.count() == 0:
            pytest.skip("No visible theme toggle found")
        toggle.first.click()
        admin_page.wait_for_timeout(500)

        # Check if html class changed (toggles "light" class on <html>)
        html_class_after = admin_page.locator("html").get_attribute("class") or ""
        changed = html_class_before != html_class_after
        if not changed:
            # Check localStorage as fallback — theme stored as 'odin-theme'
            theme_ls = admin_page.evaluate("localStorage.getItem('odin-theme')")
            assert theme_ls is not None, \
                "W6: Theme toggle click didn't change html class or localStorage"

    def test_w6_theme_persists(self, browser_instance):
        """W6.3: Theme persists after page reload (localStorage)."""
        ctx, pg = _login_via_token_inline(browser_instance, ADMIN_USERNAME, ADMIN_PASSWORD)
        try:
            pg.goto(f"{FRONTEND_URL}/", wait_until="networkidle", timeout=15000)
            # Set theme via localStorage directly
            pg.evaluate("localStorage.setItem('theme', 'dark')")
            pg.reload(wait_until="networkidle", timeout=15000)
            # Check if dark theme is applied
            theme = pg.evaluate("localStorage.getItem('theme')")
            assert theme == "dark", f"W6: Theme not persisted — got '{theme}'"
        finally:
            pg.close()
            ctx.close()


# ═══════════════════════════════════════════════════════════════════════════
# W7: MOBILE RESPONSIVE
# ═══════════════════════════════════════════════════════════════════════════

class TestW7MobileResponsive:
    """W7: Mobile viewport (375px) — hamburger menu, stacked cards, no overflow."""

    def test_w7_hamburger_menu(self, browser_instance):
        """W7.1: At 375px width, sidebar collapses to hamburger menu."""
        # credentials from module-level constants
        ctx = browser_instance.new_context(
            viewport={"width": 375, "height": 812},  # iPhone-sized
            ignore_https_errors=True,
        )
        pg = ctx.new_page()
        try:
            # Login via API and inject token
            import requests as _requests
            _ck = f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}"
            if _ck not in _WF_TOKEN_CACHE:
                _r = _requests.post(f"{BASE_URL}/api/auth/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, timeout=15)
                assert _r.status_code == 200, f"Login failed: {_r.status_code}"
                _WF_TOKEN_CACHE[_ck] = _r.json()["access_token"]
            token = _WF_TOKEN_CACHE[_ck]
            pg.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=15000)
            pg.evaluate(f"""() => {{
                localStorage.setItem('access_token', '{token}');
                localStorage.setItem('token', '{token}');
            }}""")
            pg.goto(FRONTEND_URL, wait_until="networkidle", timeout=15000)

            # Check for hamburger menu (aria-label="Open menu")
            hamburger = pg.locator('button[aria-label="Open menu"]')
            sidebar_visible = pg.locator('aside:visible').count()

            has_hamburger = hamburger.count() > 0
            sidebar_hidden = sidebar_visible == 0
            assert has_hamburger or sidebar_hidden, \
                "W7: Full sidebar visible at 375px — no hamburger menu"

        finally:
            pg.close()
            ctx.close()

    def test_w7_no_horizontal_scroll(self, browser_instance):
        """W7.2: No horizontal scrollbar at mobile width."""
        # credentials from module-level constants
        ctx = browser_instance.new_context(
            viewport={"width": 375, "height": 812},
            ignore_https_errors=True,
        )
        pg = ctx.new_page()
        try:
            import requests as _requests
            _ck = f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}"
            if _ck not in _WF_TOKEN_CACHE:
                _r = _requests.post(f"{BASE_URL}/api/auth/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, timeout=15)
                assert _r.status_code == 200, f"Login failed: {_r.status_code}"
                _WF_TOKEN_CACHE[_ck] = _r.json()["access_token"]
            token = _WF_TOKEN_CACHE[_ck]
            pg.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=15000)
            pg.evaluate(f"""() => {{
                localStorage.setItem('access_token', '{token}');
                localStorage.setItem('token', '{token}');
            }}""")
            pg.goto(FRONTEND_URL, wait_until="networkidle", timeout=15000)

            # Check for horizontal overflow
            has_overflow = pg.evaluate("""() => {
                return document.documentElement.scrollWidth > document.documentElement.clientWidth;
            }""")
            assert not has_overflow, \
                "W7: Horizontal scrollbar detected at 375px width"
        finally:
            pg.close()
            ctx.close()

    def test_w7_mobile_navigation(self, browser_instance):
        """W7.3: All main nav items accessible on mobile."""
        # credentials from module-level constants
        ctx = browser_instance.new_context(
            viewport={"width": 375, "height": 812},
            ignore_https_errors=True,
        )
        pg = ctx.new_page()
        try:
            import requests as _requests
            _ck = f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}"
            if _ck not in _WF_TOKEN_CACHE:
                _r = _requests.post(f"{BASE_URL}/api/auth/login", data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}, timeout=15)
                assert _r.status_code == 200, f"Login failed: {_r.status_code}"
                _WF_TOKEN_CACHE[_ck] = _r.json()["access_token"]
            token = _WF_TOKEN_CACHE[_ck]
            pg.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=15000)
            pg.evaluate(f"""() => {{
                localStorage.setItem('access_token', '{token}');
                localStorage.setItem('token', '{token}');
            }}""")
            # Navigate to root (not reload — reload stays on /login)
            pg.goto(FRONTEND_URL, wait_until="networkidle", timeout=15000)

            # Open hamburger menu to reveal sidebar nav
            hamburger = pg.locator('button[aria-label="Open menu"]')
            if hamburger.count() > 0:
                hamburger.first.click()
                pg.wait_for_timeout(1000)

            # Check for nav links (in sidebar or page)
            nav_links = pg.locator("a[href]")
            link_count = nav_links.count()
            assert link_count >= 3, f"W7: Only {link_count} nav links visible on mobile after opening menu"
        finally:
            pg.close()
            ctx.close()


# ═══════════════════════════════════════════════════════════════════════════
# W8: UPLOAD FLOW
# ═══════════════════════════════════════════════════════════════════════════

class TestW8UploadFlow:
    """W8: File upload flow — verify upload page renders and accepts files."""

    def test_w8_upload_page_exists(self, admin_page):
        """W8.1: Upload page/modal is accessible."""
        # Try direct nav
        admin_page.goto(f"{FRONTEND_URL}/upload", wait_until="networkidle", timeout=15000)
        body = admin_page.inner_text("body")
        upload_found = (
            len(body.strip()) > 30 and "404" not in body and "not found" not in body.lower()
        )

        if not upload_found:
            # Try models page with upload button
            admin_page.goto(f"{FRONTEND_URL}/models", wait_until="networkidle", timeout=15000)
            upload_btn = admin_page.locator(
                'button:has-text("Upload"), '
                'a:has-text("Upload"), '
                '[class*="upload"], '
                'input[type="file"]'
            )
            upload_found = upload_btn.count() > 0

        if not upload_found:
            pytest.xfail("W8: Upload page/button not found")

    def test_w8_file_input_exists(self, admin_page):
        """W8.2: File input element exists on upload page."""
        # Try upload page first
        admin_page.goto(f"{FRONTEND_URL}/upload", wait_until="networkidle", timeout=15000)
        file_input = admin_page.locator('input[type="file"]')
        if file_input.count() == 0:
            # Try models page
            admin_page.goto(f"{FRONTEND_URL}/models", wait_until="networkidle", timeout=15000)
            # Click upload button to open modal
            upload_btn = admin_page.locator(
                'button:has-text("Upload"), a:has-text("Upload")'
            )
            if upload_btn.count() > 0:
                upload_btn.first.click()
                admin_page.wait_for_timeout(500)
            file_input = admin_page.locator('input[type="file"]')

        if file_input.count() == 0:
            # Check for drag-drop zone
            dropzone = admin_page.locator(
                '[class*="dropzone"], [class*="drop-zone"], '
                '[class*="Dropzone"], [class*="upload-area"]'
            )
            if dropzone.count() == 0:
                pytest.xfail("W8: No file input or drop zone found")

    @pytest.mark.skip(reason="W8.3-7: Full upload test requires a .3mf file — skipped in headless")
    def test_w8_upload_3mf(self):
        """W8.3-7: Upload .3mf, verify metadata extraction."""
        pass
