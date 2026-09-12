from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Browser, Page, sync_playwright


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required by the candidate gate")
    return value


BASE_URL = _required("ODIN_CANDIDATE_BASE_URL").rstrip("/")
API_KEY = _required("ODIN_CANDIDATE_API_KEY")
PERSONAS = {
    "admin": (
        "candidate-admin@example.invalid",
        _required("ODIN_CANDIDATE_ADMIN_PASSWORD"),
    ),
    "operator": (
        "candidate-operator@example.invalid",
        _required("ODIN_CANDIDATE_OPERATOR_PASSWORD"),
    ),
    "viewer": (
        "candidate-viewer@example.invalid",
        _required("ODIN_CANDIDATE_VIEWER_PASSWORD"),
    ),
}


def login_via_ui(page: Page, role: str) -> None:
    username, password = PERSONAS[role]
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=20_000)
    page.locator("#login-username").fill(username)
    page.locator("#login-password").fill(password)
    page.get_by_role("button", name="Sign In", exact=True).click()
    page.wait_for_url(re.compile(r"^(?!.*\/login(?:$|[?#])).*$"), timeout=20_000)
    page.locator("#main-content").wait_for(state="visible", timeout=20_000)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture(scope="session")
def browser_instance() -> Browser:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        yield browser
        browser.close()


@pytest.fixture
def page(browser_instance: Browser, request) -> Page:
    context = browser_instance.new_context(viewport={"width": 1280, "height": 900})
    candidate_page = context.new_page()
    yield candidate_page
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        artifact_dir = Path(_required("ODIN_CANDIDATE_ARTIFACT_DIR"))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", request.node.nodeid).strip("-")
        candidate_page.screenshot(path=artifact_dir / f"{safe_name}.png", full_page=True)
    context.close()


@pytest.fixture(scope="session")
def role_storage_states(browser_instance: Browser) -> dict[str, dict]:
    states: dict[str, dict] = {}
    for role in PERSONAS:
        context = browser_instance.new_context(viewport={"width": 1280, "height": 900})
        role_page = context.new_page()
        login_via_ui(role_page, role)
        states[role] = context.storage_state()
        context.close()
    return states


@pytest.fixture
def role_page_factory(browser_instance: Browser, role_storage_states: dict[str, dict], request):
    opened: list[tuple[object, Page]] = []

    def open_page(role: str, *, mobile: bool = False) -> Page:
        viewport = {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 900}
        context = browser_instance.new_context(
            viewport=viewport,
            storage_state=role_storage_states[role],
        )
        role_page = context.new_page()
        role_page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20_000)
        role_page.locator("#main-content").wait_for(state="visible", timeout=20_000)
        opened.append((context, role_page))
        return role_page

    yield open_page
    report = getattr(request.node, "rep_call", None)
    for index, (context, role_page) in enumerate(opened):
        if report is not None and report.failed:
            artifact_dir = Path(_required("ODIN_CANDIDATE_ARTIFACT_DIR"))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", request.node.nodeid).strip("-")
            role_page.screenshot(
                path=artifact_dir / f"{safe_name}-{index}.png",
                full_page=True,
            )
        context.close()


def login(role: str) -> requests.Session:
    username, password = PERSONAS[role]
    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/api/auth/login",
        data={"username": username, "password": password},
        timeout=15,
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    session.headers.update(
        {"Authorization": f"Bearer {token}", "X-API-Key": API_KEY}
    )
    return session


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def api_key() -> str:
    return API_KEY


@pytest.fixture(scope="session")
def admin() -> requests.Session:
    with login("admin") as session:
        yield session


@pytest.fixture(scope="session")
def operator() -> requests.Session:
    with login("operator") as session:
        yield session


@pytest.fixture(scope="session")
def viewer() -> requests.Session:
    with login("viewer") as session:
        yield session
