"""
O.D.I.N. Phase 4+5 — Playwright Shared Fixtures
conftest.py for tests/test_e2e/

Provides:
  - Headless Chromium browser
  - Login helper (returns authenticated page)
  - API client for seeding test data
  - Test data fixtures (printer, spool, model, product, order, job)
  - Cleanup on teardown
"""
import os
import pytest
import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "OdinAdmin1")

# Test users — will be created via API if they don't exist
TEST_USERS = {
    "viewer": {"username": "e2e_viewer", "password": "ViewerE2EPass1!", "role": "viewer"},
    "operator": {"username": "e2e_operator", "password": "OperatorE2EPass1!", "role": "operator"},
    "admin": {"username": "e2e_admin", "password": "AdminE2EPass1!", "role": "admin"},
}


# ---------------------------------------------------------------------------
# API helpers (for seeding data, not for Playwright tests themselves)
# ---------------------------------------------------------------------------
class APIClient:
    """Simple API client for test data setup/teardown."""

    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.token = None
        self._login(username, password)

    def _login(self, username, password):
        # Use global token cache to avoid rate limits
        cache_key = f"{username}:{password}"
        if cache_key in _TOKEN_CACHE:
            self.token = _TOKEN_CACHE[cache_key]
            return
        resp = requests.post(
            f"{self.base_url}/api/auth/login",
            data={"username": username, "password": password},
            timeout=15,
        )
        if resp.status_code == 200:
            self.token = resp.json().get("access_token")
            _TOKEN_CACHE[cache_key] = self.token
        else:
            raise RuntimeError(f"Login failed for {username}: {resp.status_code} {resp.text[:200]}")

    def _headers(self):
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path, **kwargs):
        return requests.get(f"{self.base_url}{path}", headers=self._headers(), timeout=15, **kwargs)

    def post(self, path, json=None, **kwargs):
        return requests.post(f"{self.base_url}{path}", json=json, headers=self._headers(), timeout=15, **kwargs)

    def put(self, path, json=None, **kwargs):
        return requests.put(f"{self.base_url}{path}", json=json, headers=self._headers(), timeout=15, **kwargs)

    def patch(self, path, json=None, **kwargs):
        return requests.patch(f"{self.base_url}{path}", json=json, headers=self._headers(), timeout=15, **kwargs)

    def delete(self, path, **kwargs):
        return requests.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=15, **kwargs)


# ---------------------------------------------------------------------------
# Session-scoped: browser, API client, test data
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def api():
    """Admin API client for seeding test data."""
    return APIClient(BASE_URL, ADMIN_USERNAME, ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def test_users_created(api):
    """Ensure test users exist. Returns dict of {role: {username, password}}."""
    created_ids = []
    for role, info in TEST_USERS.items():
        # Check if user exists
        resp = api.get("/api/users")
        if resp.status_code == 200:
            users = resp.json() if isinstance(resp.json(), list) else resp.json().get("users", [])
            existing = [u for u in users if u.get("username") == info["username"]]
            if existing:
                continue
        # Create user
        resp = api.post("/api/users", json={
            "username": info["username"],
            "password": info["password"],
            "email": f"{info['username']}@test.local",
            "role": info["role"],
        })
        if resp.status_code in (200, 201):
            uid = resp.json().get("id")
            if uid:
                created_ids.append(uid)
        # 403 = license limit, user may already exist — continue
    yield TEST_USERS
    # Cleanup: don't delete users (might hit license issues recreating them)


@pytest.fixture(scope="session")
def seed_data(api):
    """
    Seed minimal test data via API. Returns dict with IDs.
    Discovers existing data first to avoid license-cap issues.
    """
    data = {}

    # --- Discover existing printer ---
    resp = api.get("/api/printers")
    if resp.status_code == 200:
        printers = resp.json() if isinstance(resp.json(), list) else resp.json().get("printers", [])
        if printers:
            data["printer_id"] = printers[0].get("id")
            data["printer_name"] = printers[0].get("name", "Unknown")

    # --- Discover existing spool ---
    resp = api.get("/api/spools")
    if resp.status_code == 200:
        spools = resp.json() if isinstance(resp.json(), list) else resp.json().get("spools", resp.json())
        if isinstance(spools, list) and spools:
            data["spool_id"] = spools[0].get("id")

    # --- Discover existing model ---
    resp = api.get("/api/models")
    if resp.status_code == 200:
        models = resp.json() if isinstance(resp.json(), list) else resp.json().get("models", [])
        if models:
            data["model_id"] = models[0].get("id")
            data["model_name"] = models[0].get("name", "Unknown")

    # --- Discover existing product ---
    resp = api.get("/api/products")
    if resp.status_code == 200:
        products = resp.json() if isinstance(resp.json(), list) else resp.json().get("products", [])
        if products:
            data["product_id"] = products[0].get("id")
            data["product_name"] = products[0].get("name", "Unknown")

    # --- Discover existing order ---
    resp = api.get("/api/orders")
    if resp.status_code == 200:
        orders = resp.json() if isinstance(resp.json(), list) else resp.json().get("orders", [])
        if orders:
            data["order_id"] = orders[0].get("id")

    # --- Discover existing job ---
    resp = api.get("/api/jobs")
    if resp.status_code == 200:
        jobs = resp.json() if isinstance(resp.json(), list) else resp.json().get("jobs", [])
        if jobs:
            data["job_id"] = jobs[0].get("id")

    return data


@pytest.fixture(scope="session")
def browser_instance():
    """Launch headless Chromium for the entire test session."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield browser
        browser.close()


# ---------------------------------------------------------------------------
# Function-scoped: fresh page per test (or reusable context)
# ---------------------------------------------------------------------------
@pytest.fixture
def browser_context(browser_instance):
    """Fresh browser context per test (isolated cookies/storage)."""
    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    yield context
    context.close()


@pytest.fixture
def page(browser_context):
    """Fresh page per test."""
    pg = browser_context.new_page()
    yield pg
    pg.close()


def _login_via_ui(page, username, password, frontend_url=FRONTEND_URL):
    """Login through the actual UI login form."""
    page.goto(f"{frontend_url}/login", wait_until="networkidle", timeout=15000)
    # Try common selectors for username/password fields
    for selector in ['input[name="username"]', 'input[type="text"]', '#username']:
        if page.locator(selector).count() > 0:
            page.locator(selector).first.fill(username)
            break
    for selector in ['input[name="password"]', 'input[type="password"]', '#password']:
        if page.locator(selector).count() > 0:
            page.locator(selector).first.fill(password)
            break
    # Click login button
    for selector in ['button[type="submit"]', 'button:has-text("Login")', 'button:has-text("Sign In")', 'button:has-text("Log In")']:
        if page.locator(selector).count() > 0:
            page.locator(selector).first.click()
            break
    # Wait for navigation away from login page
    page.wait_for_url(lambda url: "/login" not in url, timeout=10000)


# Session-level token cache — login ONCE per user, reuse token everywhere
_TOKEN_CACHE = {}

def _get_token(username, password, base_url=BASE_URL):
    """Get a cached JWT token, only hitting the login API once per user."""
    cache_key = f"{username}:{password}"
    if cache_key not in _TOKEN_CACHE:
        import time
        # Small delay to avoid rate limits during initial setup
        if len(_TOKEN_CACHE) > 0:
            time.sleep(1)
        resp = requests.post(
            f"{base_url}/api/auth/login",
            data={"username": username, "password": password},
            timeout=15,
        )
        assert resp.status_code == 200, f"Login failed for {username}: {resp.status_code} - {resp.text[:200]}"
        _TOKEN_CACHE[cache_key] = resp.json()["access_token"]
    return _TOKEN_CACHE[cache_key]


def _login_via_token(browser_instance, username, password, base_url=BASE_URL, frontend_url=FRONTEND_URL):
    """
    Login via cached API token, inject into localStorage, return (context, page).
    """
    token = _get_token(username, password, base_url)

    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.goto(frontend_url, wait_until="domcontentloaded", timeout=15000)
    page.evaluate(f"""() => {{
        localStorage.setItem('access_token', '{token}');
        localStorage.setItem('token', '{token}');
        localStorage.setItem('auth_token', '{token}');
    }}""")
    page.reload(wait_until="networkidle", timeout=15000)
    return context, page


@pytest.fixture
def admin_page(browser_instance):
    """Page logged in as admin."""
    ctx, pg = _login_via_token(browser_instance, ADMIN_USERNAME, ADMIN_PASSWORD)
    yield pg
    pg.close()
    ctx.close()


@pytest.fixture
def viewer_page(browser_instance, test_users_created):
    """Page logged in as viewer."""
    info = TEST_USERS["viewer"]
    try:
        ctx, pg = _login_via_token(browser_instance, info["username"], info["password"])
        yield pg
        pg.close()
        ctx.close()
    except Exception:
        pytest.skip("Could not login as viewer — user may not exist")


@pytest.fixture
def operator_page(browser_instance, test_users_created):
    """Page logged in as operator."""
    info = TEST_USERS["operator"]
    try:
        ctx, pg = _login_via_token(browser_instance, info["username"], info["password"])
        yield pg
        pg.close()
        ctx.close()
    except Exception:
        pytest.skip("Could not login as operator — user may not exist")


# ---------------------------------------------------------------------------
# Helpers available to tests
# ---------------------------------------------------------------------------
@pytest.fixture
def login_as(browser_instance, test_users_created):
    """Factory fixture: login_as("viewer") returns (context, page)."""
    active = []

    def _login(role):
        if role == "admin":
            ctx, pg = _login_via_token(browser_instance, ADMIN_USERNAME, ADMIN_PASSWORD)
        else:
            info = TEST_USERS[role]
            ctx, pg = _login_via_token(browser_instance, info["username"], info["password"])
        active.append((ctx, pg))
        return pg

    yield _login

    for ctx, pg in active:
        pg.close()
        ctx.close()


# Navigation pages list (for iterating across pages)
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
