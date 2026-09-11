"""Deterministic compiled-frontend EDU entitlement checks in Chromium."""

import json
import os
from urllib.parse import urlparse

from playwright.sync_api import Route, sync_playwright


BASE_URL = os.environ.get("EDU_FRONTEND_URL", "http://127.0.0.1:4173")


def _api_handler(features: list[str]):
    def handle(route: Route) -> None:
        path = urlparse(route.request.url).path
        if path == "/api/license":
            body = {
                "valid": True,
                "tier": "education" if "print_quotas" in features else "pro",
                "features": features,
                "max_printers": 20,
                "max_users": 500,
                "managed_externally": True,
            }
        elif path == "/api/auth/me":
            body = {"id": 1, "username": "edu-admin", "role": "admin", "group_id": None}
        elif path == "/api/setup/status":
            body = {"needs_setup": False}
        elif path == "/api/permissions":
            body = {}
        elif path in {"/api/admin/quotas", "/api/groups", "/api/printers", "/api/users", "/api/orgs"}:
            body = []
        elif path == "/api/pricing-config":
            body = {"ui_mode": "advanced"}
        elif path == "/api/config/require-job-approval":
            body = {"require_job_approval": True}
        elif path == "/api/alerts/unread-count":
            body = {"count": 0}
        else:
            body = {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    return handle


def _open_settings(playwright, features: list[str]):
    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context()
    context.add_init_script(
        "localStorage.setItem('odin_user', JSON.stringify({username:'edu-admin', role:'admin'}))"
    )
    page = context.new_page()
    browser_errors: list[str] = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    page.on(
        "console",
        lambda message: browser_errors.append(message.text)
        if message.type == "error" else None,
    )
    page.route("**/api/**", _api_handler(features))
    page.goto(f"{BASE_URL}/settings", wait_until="domcontentloaded")
    page.get_by_text("Configure your print farm").wait_for()
    return browser, page, browser_errors


def test_education_contract_shows_approval_and_quota_controls():
    with sync_playwright() as playwright:
        browser, page, browser_errors = _open_settings(playwright, [
            "rbac", "permissions", "job_approval", "user_groups",
            "print_quotas", "usage_reports",
        ])
        try:
            page.get_by_text("Job Approval Workflow", exact=True).wait_for()
            page.get_by_role("button", name="Access").click()
            page.get_by_role("button", name="Quotas & Restrictions").click()
            page.get_by_text("Print Quotas", exact=True).wait_for()
            assert browser_errors == []
        finally:
            browser.close()


def test_pro_contract_hides_education_only_controls():
    with sync_playwright() as playwright:
        browser, page, browser_errors = _open_settings(playwright, ["rbac", "permissions", "analytics"])
        try:
            assert page.get_by_text("Job Approval Workflow", exact=True).count() == 0
            page.get_by_role("button", name="Access").click()
            page.get_by_role("button", name="Quotas & Restrictions").click()
            assert page.get_by_text("Print Quotas", exact=True).count() == 0
            assert browser_errors == []
        finally:
            browser.close()
