from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

from .conftest import BASE_URL, PERSONAS


FIXTURE = Path(__file__).parents[1] / "fixtures" / "release_gate" / "candidate-calibration-cube.3mf"


def _goto(page: Page, path: str) -> None:
    page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded", timeout=20_000)


def test_login_feedback_valid_login_and_logout(page: Page):
    _goto(page, "/login")
    page.locator("#login-username").fill(PERSONAS["admin"][0])
    page.locator("#login-password").fill("definitely-wrong")
    page.get_by_role("button", name="Sign In", exact=True).click()
    expect(page.get_by_text("Invalid credentials", exact=True)).to_be_visible()

    page.locator("#login-password").fill(PERSONAS["admin"][1])
    page.get_by_role("button", name="Sign In", exact=True).click()
    expect(page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible(timeout=20_000)
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page.get_by_role("heading", name="Confirm Logout", exact=True)).to_be_visible()
    page.get_by_role("button", name="Logout", exact=True).last.click()
    page.wait_for_url(f"{BASE_URL}/login", timeout=20_000)


def test_admin_operational_navigation_and_seeded_content(role_page_factory):
    page = role_page_factory("admin")
    checks = (
        ("/printers", "Printers", "Synthetic / No Hardware"),
        ("/jobs", "Jobs", "ODIN Candidate Cube"),
        ("/models", "Models", "ODIN Candidate Calibration Cube"),
        ("/spools", "Spools", "ODIN Candidate"),
        ("/upload", "Upload Print File", "Drop .3mf file here"),
    )
    for path, heading, content in checks:
        _goto(page, path)
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible(timeout=20_000)
        expect(page.get_by_text(content, exact=False).first).to_be_visible(timeout=20_000)


def test_community_gates_and_access_tab_behavior(role_page_factory):
    page = role_page_factory("admin")
    for path in ("/products", "/orders"):
        _goto(page, path)
        expect(page.get_by_role("heading", name="Pro Feature", exact=True)).to_be_visible(timeout=20_000)

    _goto(page, "/settings")
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_be_visible()
    page.get_by_role("button", name="Access", exact=True).click()
    expect(page.get_by_text("Users & Groups", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_be_visible()


def test_operator_navigation_and_admin_route_enforcement(role_page_factory):
    page = role_page_factory("operator")
    _goto(page, "/jobs")
    expect(page.get_by_role("heading", name="Jobs", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("button", name="New Job", exact=True)).to_be_visible()
    _goto(page, "/settings")
    page.wait_for_url(BASE_URL + "/", timeout=20_000)
    expect(page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible()


def test_viewer_read_only_controls_and_admin_route_enforcement(role_page_factory):
    page = role_page_factory("viewer")
    _goto(page, "/jobs")
    expect(page.get_by_role("heading", name="Jobs", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("button", name="New Job", exact=True)).to_have_count(0)
    _goto(page, "/printers")
    expect(page.get_by_text("Synthetic / No Hardware", exact=False).first).to_be_visible(timeout=20_000)
    expect(page.get_by_role("button", name="Add Printer", exact=True)).to_have_count(0)
    _goto(page, "/settings")
    page.wait_for_url(BASE_URL + "/", timeout=20_000)


def test_mobile_navigation_and_horizontal_layout(role_page_factory):
    page = role_page_factory("admin", mobile=True)
    page.get_by_role("button", name="Open menu", exact=True).click()
    drawer = page.get_by_label("Main navigation")
    expect(drawer.get_by_role("button", name="Close menu", exact=True)).to_be_visible()
    drawer.get_by_role("link", name="Printers", exact=True).click()
    expect(page.get_by_role("heading", name="Printers", exact=True)).to_be_visible(timeout=20_000)
    dimensions = page.evaluate(
        "() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth})"
    )
    assert dimensions["scroll"] <= dimensions["client"] + 1, dimensions


def test_theme_persists_and_keyboard_skip_link_works(role_page_factory):
    page = role_page_factory("admin")
    toggle = page.get_by_role("button", name="Switch to light mode", exact=True)
    if toggle.count() == 0:
        toggle = page.get_by_role("button", name="Switch to dark mode", exact=True)
    original_label = toggle.get_attribute("aria-label")
    toggle.click()
    changed_label = "Switch to dark mode" if original_label == "Switch to light mode" else "Switch to light mode"
    expect(page.get_by_role("button", name=changed_label, exact=True)).to_be_visible()
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("button", name=changed_label, exact=True)).to_be_visible(timeout=20_000)

    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#main-content")).to_be_focused()


def test_real_3mf_upload_reaches_model_library(role_page_factory):
    assert FIXTURE.is_file()
    page = role_page_factory("admin")
    _goto(page, "/upload")
    page.locator("#file-upload").set_input_files(str(FIXTURE))
    expect(page.get_by_text("Added to Model Library", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="ODIN Candidate Upload Cube", exact=True)).to_be_visible()
    page.get_by_role("button", name="Quick Print", exact=True).click()
    expect(page.get_by_role("button", name="Queued!", exact=True)).to_be_visible(timeout=20_000)
    _goto(page, "/jobs")
    row = page.get_by_role("row").filter(has_text="ODIN Candidate Upload Cube")
    expect(row).to_be_visible(timeout=20_000)
    expect(row).to_contain_text("pending")


def test_seeded_vision_detection_state(role_page_factory):
    page = role_page_factory("admin")
    _goto(page, "/detections")
    expect(page.get_by_role("heading", name="Vigil AI Detections", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("1 detection", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("Spaghetti", exact=True).first).to_be_visible()
