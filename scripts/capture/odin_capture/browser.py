"""Playwright control — launch headless Chromium, install the demo session
cookie, navigate to a target route, wait for `data-capture` selectors, and
capture a screenshot.

Headless-only by directive (the spec's hard stops include "use headless
from start"). Anything that opts out is gated behind an explicit
`headless=False` arg.

Playwright import is deferred inside `capture_screenshot` so that
`python -m scripts.capture.odin_capture.cli list-scenes` keeps running on
hosts that haven't `pip install`-ed playwright yet (Wake 1 acceptance).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .auth import SessionCookie

logger = logging.getLogger("odin_capture.browser")


@dataclass
class Viewport:
    width: int
    height: int


class BrowserError(RuntimeError):
    """Raised when Playwright launch, navigation, or capture fails."""


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except ImportError as e:
        raise BrowserError(
            "playwright not installed. "
            "Run: pip install -r scripts/capture/requirements.txt && "
            "playwright install chromium"
        ) from e


def capture_screenshot(
    base_url: str,
    route: str,
    viewport: Viewport,
    cookie: SessionCookie,
    wait_selector: str,
    out_path: Path,
    *,
    headless: bool = True,
    full_page: bool = False,
    wait_timeout_ms: int = 30_000,
    color_scheme: str = "dark",
    extra_settle_ms: int = 1500,
    min_visible_printers: Optional[int] = None,
) -> Path:
    """Launch Chromium, log in via cookie, navigate, wait, screenshot.

    `extra_settle_ms` lets the dashboard's `useQuery` refetches finish
    after the first telemetry tick lands. The Pydantic Scene model
    surfaces `min_visible_printers` so capture fails loud if the
    fleet-grid renders <N children (catches "telemetry didn't arrive
    in time" scenarios where the page exists but is empty).
    """
    sync_playwright = _require_playwright()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    target_url = base_url.rstrip("/") + route

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": viewport.width, "height": viewport.height},
                color_scheme=color_scheme,
                device_scale_factor=2,  # retina output for marketing assets
            )
            context.add_cookies([cookie.to_playwright_cookie()])

            page = context.new_page()
            logger.info("navigating to %s", target_url)
            page.goto(target_url, wait_until="networkidle", timeout=wait_timeout_ms)

            logger.info("waiting for selector %r", wait_selector)
            page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)

            if min_visible_printers is not None:
                # The fleet grid is the wait_selector; its children are the
                # PrinterCards. Count them so we fail loud on an empty grid.
                children = page.locator(f"{wait_selector} > *")
                count = children.count()
                if count < min_visible_printers:
                    raise BrowserError(
                        f"only {count} printers visible under {wait_selector!r}, "
                        f"expected ≥ {min_visible_printers}. "
                        "Likely cause: telemetry hasn't arrived from the publisher yet, "
                        "or the scenario printers aren't registered in the backend DB."
                    )

            # Let in-flight network refetches settle. Dashboard polls every
            # 30s but the first paint can lag the initial query.
            page.wait_for_timeout(extra_settle_ms)

            logger.info("screenshotting → %s", out_path)
            page.screenshot(path=str(out_path), full_page=full_page)
            return out_path
        finally:
            browser.close()
