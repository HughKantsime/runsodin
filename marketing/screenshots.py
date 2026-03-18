#!/usr/bin/env python3
"""
Playwright screenshot automation for ODIN marketing assets.

Logs into ODIN and captures screenshots of every page in dark + light mode
at desktop and mobile viewports.

Environment variables:
    ODIN_BASE_URL       Base URL of the ODIN instance (default: http://localhost:8000)
    ODIN_ADMIN_USER     Admin username (default: admin)
    ODIN_ADMIN_PASSWORD Admin password (required)
"""

import base64
import json
import logging
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("ODIN_BASE_URL", "http://localhost:8000")
ADMIN_USER = os.environ.get("ODIN_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ODIN_ADMIN_PASSWORD", "")

PAGES = [
    ("dashboard", "/"),
    ("printers", "/printers"),
    ("jobs", "/jobs"),
    ("timeline", "/timeline"),
    ("models", "/models"),
    ("spools", "/spools"),
    ("orders", "/orders"),
    ("products", "/products"),
    ("archives", "/archives"),
    ("print-log", "/print-log"),
    ("analytics", "/analytics"),
    ("cameras", "/cameras"),
    ("settings", "/settings"),
    ("alerts", "/alerts"),
    ("calculator", "/calculator"),
]

DESKTOP_VIEWPORT = {"width": 1920, "height": 1080}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
MOBILE_PAGES = {"dashboard", "printers", "jobs", "spools", "orders"}

THEMES = ["dark", "light"]

OUTPUT_DIR = Path(__file__).resolve().parent / "screenshots"

NAV_TIMEOUT = 20_000  # 20 seconds
SETTLE_DELAY = 1500   # 1.5 seconds for animations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def login(base_url: str, username: str, password: str) -> str:
    """Authenticate via the ODIN API and return the JWT token."""
    resp = requests.post(
        f"{base_url}/api/auth/login",
        data={"username": username, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token") or resp.json().get("token")
    if not token:
        raise RuntimeError(f"No token in login response: {resp.json()}")
    return token


def decode_jwt_payload(token: str) -> dict:
    """Decode the payload segment of a JWT (no verification)."""
    payload_b64 = token.split(".")[1]
    # Pad to a multiple of 4
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def inject_auth(page, token: str) -> None:
    """Inject JWT and user info into browser localStorage."""
    payload = decode_jwt_payload(token)
    user_json = json.dumps({
        "username": payload.get("sub", "admin"),
        "role": payload.get("role", "admin"),
    })
    page.evaluate(f"""() => {{
        localStorage.setItem('token', '{token}');
        localStorage.setItem('user', {json.dumps(user_json)});
    }}""")


def capture_page(page, name: str, path: str, theme: str, viewport_label: str,
                 output_dir: Path, base_url: str) -> bool:
    """Navigate to a page and save a screenshot. Returns True on success."""
    filename = f"{name}-{theme}-{viewport_label}.png"
    dest = output_dir / filename
    try:
        page.goto(f"{base_url}{path}", wait_until="networkidle", timeout=NAV_TIMEOUT)
        page.wait_for_timeout(SETTLE_DELAY)
        page.screenshot(path=str(dest), full_page=False)
        logger.info("OK  %s", filename)
        return True
    except Exception as exc:
        logger.error("FAIL %s — %s", filename, exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not ADMIN_PASSWORD:
        logger.error("ODIN_ADMIN_PASSWORD environment variable is required")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Logging in to %s as %s …", BASE_URL, ADMIN_USER)
    token = login(BASE_URL, ADMIN_USER, ADMIN_PASSWORD)
    logger.info("Login successful — token obtained")

    total = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        for theme in THEMES:
            color_scheme = theme  # "dark" or "light"

            # ---- Desktop ----
            ctx = browser.new_context(
                viewport=DESKTOP_VIEWPORT,
                color_scheme=color_scheme,
            )
            page = ctx.new_page()

            # Navigate to base URL first so localStorage is on the right origin
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            inject_auth(page, token)
            # Also persist theme preference in localStorage
            page.evaluate(f"() => localStorage.setItem('theme', '{theme}')")

            for name, path in PAGES:
                if capture_page(page, name, path, theme, "desktop", OUTPUT_DIR, BASE_URL):
                    total += 1

            ctx.close()

            # ---- Mobile ----
            ctx_mobile = browser.new_context(
                viewport=MOBILE_VIEWPORT,
                color_scheme=color_scheme,
            )
            mpage = ctx_mobile.new_page()

            mpage.goto(BASE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            inject_auth(mpage, token)
            mpage.evaluate(f"() => localStorage.setItem('theme', '{theme}')")

            for name, path in PAGES:
                if name not in MOBILE_PAGES:
                    continue
                if capture_page(mpage, name, path, theme, "mobile", OUTPUT_DIR, BASE_URL):
                    total += 1

            ctx_mobile.close()

        browser.close()

    logger.info("Done — %d screenshots saved to %s", total, OUTPUT_DIR)


if __name__ == "__main__":
    main()
