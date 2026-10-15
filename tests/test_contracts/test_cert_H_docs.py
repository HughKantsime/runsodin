"""Cert group H — F22-22.6-01..04 documentation wiki, rendered in headless Chromium.

CERT_H_DOCS_BUILD: a `docusaurus build` output of odin-site/docs built from a scratch
copy (served here on 127.0.0.1:27461). CERT_H_NETWORK=1 additionally checks the live
docs.runsodin.com read-only.
"""
from __future__ import annotations

import functools
import http.server
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE_DOCS = ROOT.parent / "odin-site/docs"
BUILD = os.environ.get("CERT_H_DOCS_BUILD", "")
LIVE = "https://docs.runsodin.com"
NETWORK = os.environ.get("CERT_H_NETWORK") == "1"
CATEGORIES = {
    "introduction": "introduction", "getting-started": "getting started", "configuration": "configuration",
    "security": "security", "features": "features", "companion-apps": "companion apps",
    "integrations": "integrations", "api": "api reference", "reference": "api reference",
    "troubleshooting": "troubleshooting", "contributing": "contributing",
}
CLAIMED = {"introduction", "getting started", "configuration", "security", "features", "companion apps",
           "integrations", "api reference", "troubleshooting"}
SETTINGS_HELP = re.search(r"'/settings': \{ url: '([^']+)'", (ROOT / "frontend/src/utils/helpLinks.ts").read_text()).group(1)

pytestmark = pytest.mark.skipif(not BUILD, reason="CERT_H_DOCS_BUILD required")


def _ids() -> list[str]:
    ids = re.findall(r"'([a-z0-9\-/]+)'", (SITE_DOCS / "sidebars.js").read_text())
    return [i for i in ids if i not in ("category", "docs")]


@pytest.fixture(scope="module")
def local_base():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=BUILD)
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 27461), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:27461"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


def _bases(local_base):
    return [local_base] + ([LIVE] if NETWORK else [])


def test_site_is_docusaurus_3_configured_for_docs_runsodin_com(local_base, browser):
    assert "url: 'https://docs.runsodin.com'" in (SITE_DOCS / "docusaurus.config.js").read_text()
    for base in _bases(local_base):
        page = browser.new_page()
        resp = page.goto(base + "/", wait_until="domcontentloaded")
        assert resp.status == 200, base
        generator = page.locator('meta[name="generator"]').get_attribute("content")
        assert re.fullmatch(r"Docusaurus v3\.\d+\.\d+", generator or ""), (base, generator)
        page.close()


def test_every_sidebar_page_renders_and_claimed_categories_are_covered(local_base, browser):
    covered = set()
    for base in _bases(local_base):
        page = browser.new_page()
        for doc_id in _ids():
            path = "/" if doc_id == "introduction" else f"/{doc_id}"
            resp = page.goto(base + path, wait_until="domcontentloaded")
            assert resp.status == 200, (base, path)
            h1 = page.locator("article h1, header h1").first
            assert h1.count() and h1.inner_text().strip(), (base, path)
            covered.add(CATEGORIES[doc_id.split("/")[0]])
        page.close()
    assert CLAIMED <= covered, CLAIMED - covered


def _theme(browser, base: str, scheme: str):
    ctx = browser.new_context(color_scheme=scheme)
    page = ctx.new_page()
    page.goto(base + "/", wait_until="domcontentloaded")
    theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    primary = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--ifm-color-primary').trim()")
    ctx.close()
    return theme, primary.upper()


@pytest.mark.parametrize("scheme", ["light", "no-preference", "dark"])
def test_dark_mode_is_default_with_odin_brand_amber_in_current_build(local_base, browser, scheme):
    assert _theme(browser, local_base, scheme) == ("dark", "#C47A1A"), scheme


@pytest.mark.skipif(not NETWORK, reason="CERT_H_NETWORK=1 required")
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: live https://docs.runsodin.com still serves a build with colorMode.respectPrefersColorScheme=true "
    "(odin-site commit 986c846 'docs default to dark' is not deployed). Scenario: a visitor whose OS is in light "
    "mode (or reports no preference) opens docs.runsodin.com -> data-theme=light, primary #9A5E0D, not dark."))
@pytest.mark.parametrize("scheme", ["light", "no-preference"])
def test_live_docs_site_defaults_to_dark(browser, scheme):
    assert _theme(browser, LIVE, scheme) == ("dark", "#C47A1A"), scheme


def test_live_docs_site_dark_scheme_uses_brand_amber(browser):
    if not NETWORK:
        pytest.skip("CERT_H_NETWORK=1 required")
    assert _theme(browser, LIVE, "dark") == ("dark", "#C47A1A")


def _status(url: str) -> int:
    ctx = None
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        pass
    try:
        with urllib.request.urlopen(url, timeout=30, context=ctx) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_settings_documentation_link_resolves_to_a_docs_page(local_base):
    assert SETTINGS_HELP.startswith(LIVE)
    path = SETTINGS_HELP[len(LIVE):]
    assert (Path(BUILD) / path.strip("/") / "index.html").exists() or (Path(BUILD) / (path.strip("/") + ".html")).exists(), path
    if NETWORK:
        assert _status(SETTINGS_HELP) == 200
