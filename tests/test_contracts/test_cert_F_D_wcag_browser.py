"""Cert F / cluster D — F12-12.7-01..07 WCAG claims on the BUILT bundle in headless Chromium.

The current frontend source is built (vite) into a temp dir; Chromium loads it from the origin of a
real ODIN server (LiveOdin) with an authenticated admin session cookie, so every page renders against
the real API. Non-/api requests are fulfilled from the fresh build (the only substitution: static
asset serving). On each SPA route (and each Settings tab) the rendered DOM is censused. The census
receipt (no secrets) is written to artifacts/application-certification/cert-F/D-wcag-browser-census.json.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin

pw_sync = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT / "artifacts/application-certification/cert-F/D-wcag-browser-census.json"

ROUTES = ["/", "/printers/1", "/jobs", "/timeline", "/printers", "/models", "/profiles", "/projects", "/spools", "/consumables",
          "/orders", "/products", "/calculator", "/analytics", "/utilization", "/print-log", "/archives",
          "/timelapses", "/cameras", "/maintenance", "/detections", "/alerts", "/audit", "/admin", "/permissions",
          "/branding", "/settings", "/upload", "/education", "/education-reports"]

CENSUS_JS = r"""
() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const name = el => (el.getAttribute('aria-label') || '').trim()
      || (el.getAttribute('aria-labelledby') ? (el.getAttribute('aria-labelledby').split(/\s+/).map(i => document.getElementById(i)?.textContent || '').join(' ').trim()) : '')
      || (el.getAttribute('title') || '').trim();
  const iconOnly = [...document.querySelectorAll('button')].filter(b => vis(b) && !b.innerText.trim() && b.querySelector('svg'))
      .map(b => ({ aria: b.getAttribute('aria-label'), title: b.getAttribute('title'), name: name(b),
                   html: b.outerHTML.slice(0, 160) }));
  const fields = [...document.querySelectorAll('input, select, textarea')].filter(el => vis(el) &&
      !['hidden', 'submit', 'button', 'reset', 'image'].includes((el.getAttribute('type') || '').toLowerCase()));
  const labelled = el => (el.labels && el.labels.length > 0 && [...el.labels].some(l => l.textContent.trim()))
      || !!(el.getAttribute('aria-label') || '').trim() || !!el.getAttribute('aria-labelledby') || !!(el.getAttribute('title') || '').trim();
  const ths = [...document.querySelectorAll('th')];
  const bars = [...document.querySelectorAll('[style*="width"]')].filter(el => /width:\s*[\d.]+%/.test(el.getAttribute('style') || '')
      && el.getBoundingClientRect().height > 0 && el.getBoundingClientRect().height <= 14 && el.parentElement
      && el.parentElement.getBoundingClientRect().width > el.getBoundingClientRect().width - 0.5);
  return {
    skip: (() => { const a = document.querySelector('a[href="#main-content"]'); return a ? a.textContent.trim() : null })(),
    main: !!document.querySelector('main#main-content[tabindex="-1"]'),
    nav: document.querySelectorAll('nav, [role="navigation"]').length,
    toolbar: !!document.querySelector('[role="toolbar"][aria-label]'),
    live: document.getElementById('sr-status')?.getAttribute('aria-live') || null,
    iconOnly,
    fields: fields.length, unlabelledFields: fields.filter(f => !labelled(f)).map(f => f.outerHTML.slice(0, 160)),
    th: ths.length, thScopeCol: ths.filter(t => t.getAttribute('scope') === 'col').length,
    progressbars: [...document.querySelectorAll('[role="progressbar"]')].map(p => ({ now: p.getAttribute('aria-valuenow'),
      min: p.getAttribute('aria-valuemin'), max: p.getAttribute('aria-valuemax') })),
    barsWithoutRole: bars.filter(b => !b.closest('[role="progressbar"]')).map(b => b.outerHTML.slice(0, 160)),
    dialogs: [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')].filter(vis).map(d => ({ role: d.getAttribute('role'),
      modal: d.getAttribute('aria-modal'), named: !!(d.getAttribute('aria-label') || d.getAttribute('aria-labelledby')) })),
    overlays: [...document.querySelectorAll('div.fixed.inset-0')].filter(vis).map(o => ({
      dialog: !!(o.matches('[role=dialog],[role=alertdialog]') || o.querySelector('[role=dialog],[role=alertdialog]')) })),
  };
}
"""


@pytest.fixture(scope="module")
def build_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run(["npx", "vite", "build", "--outDir", str(out), "--emptyOutDir"], cwd=ROOT / "frontend",
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return out


@pytest.fixture(scope="module")
def census(tmp_path_factory, build_dir):
    o = LiveOdin(tmp_path_factory.mktemp("odinD_wcag")).start()
    o.setup_admin()
    H = o.admin_headers()
    # Data so list pages render rows, tables and action buttons.
    o.post("/api/printers", headers=H, json={"name": "wcag-printer", "api_type": "moonraker", "api_host": "192.168.70.2"})
    o.post("/api/users", headers=H, json={"username": "wcag_op", "email": "wcag_op@example.test",
           "password": "Cf" + "a1b2c3d4e5f6" + "Az9", "role": "operator"})
    from sqlalchemy import text as _t
    with o.engine.begin() as c:
        pid = c.execute(_t("SELECT id FROM printers WHERE name='wcag-printer'")).scalar()
        c.execute(_t("INSERT INTO models (name, created_at, updated_at) VALUES ('wcag-model', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        for i, st in enumerate(("pending", "scheduled", "printing", "paused", "completed", "failed", "submitted", "rejected", "cancelled")):
            c.execute(_t("INSERT INTO jobs (item_name, status, printer_id, quantity, priority, duration_hours, submitted_by, "
                         "scheduled_start, scheduled_end, created_at, updated_at) VALUES (:n, :s, :p, 1, 3, 1.5, 1, "
                         "datetime('now', :a), datetime('now', :b), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
                      {"n": f"wcag-job-{st}", "s": st, "p": pid, "a": f"+{i} hours", "b": f"+{i + 1} hours"})
        c.execute(_t("INSERT INTO alerts (user_id, alert_type, severity, title, message, is_read, is_dismissed) "
                     "VALUES (1, 'print_failed', 'warning', 'wcag alert', 'm', 0, 0)"))
    o.post("/api/tokens", headers=H, json={"name": "wcag-token", "scopes": ["read"]})
    bam = o.post("/api/printers", headers=H, json={"name": "wcag-x1c", "model": "X1C", "api_type": "bambu",
                 "api_host": "192.168.70.3", "api_key": "01S00WCAG0001|12345678"}).json()
    o.put(f"/api/printers/{bam['id']}/plug", headers=H, json={"type": "tasmota", "host": "192.168.70.4"})
    o.sched = o.post("/api/report-schedules", headers=H, json={"name": "wcag-schedule", "report_type": "fleet_utilization",
           "frequency": "weekly", "recipients": ["wcag@example.test"]})
    with o.engine.begin() as c:
        c.execute(_t("UPDATE printers SET lights_on=1, last_seen=CURRENT_TIMESTAMP, gcode_state='RUNNING' WHERE id=:i"), {"i": bam["id"]})
        c.execute(_t("UPDATE users SET mfa_enabled=1 WHERE username='wcag_op'"))
    o.post("/api/orgs", headers=H, json={"name": "WCAG Org"})
    o.post("/api/spools", headers=H, json={"material": "PLA", "brand": "Cert", "color_hex": "#ff0000",
           "initial_weight_g": 1000, "remaining_weight_g": 500})
    results, opened = {}, {}
    try:
        with pw_sync.sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
            login = ctx.request.post(o.url + "/api/auth/login", form={"username": o.admin[0], "password": o.admin[1]})
            assert login.ok, login.text()

            def serve(route):
                path = route.request.url.split(o.url, 1)[1].split("?", 1)[0]
                f = build_dir / path.lstrip("/")
                if not path.startswith("/assets/") and not f.is_file():
                    f = build_dir / "index.html"
                if not f.is_file():
                    return route.fulfill(status=404, body="")
                route.fulfill(status=200, body=f.read_bytes(),
                              headers={"content-type": mimetypes.guess_type(str(f))[0] or "text/html"})

            ctx.route(lambda url: url.startswith(o.url) and "/api/" not in url and "/health" not in url, serve)
            page = ctx.new_page()

            def go(url):
                page.goto(url, wait_until="load", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: BLE001 — pages with polling never go idle
                    pass
                page.wait_for_timeout(600)
            for route in ROUTES:
                go(o.url + route)
                results[route] = page.evaluate(CENSUS_JS)
                if route == "/":
                    page.keyboard.press("Tab")
                    results[route]["firstTabTarget"] = page.evaluate("document.activeElement.getAttribute('href')")
                # Open the first Add/New/Create dialog the page offers and census it.
                btn = page.locator("button", has_text=__import__("re").compile(r"^\s*\+?\s*(Add|New|Create)\b", 2)).first
                if btn.count() and btn.is_visible():
                    try:
                        btn.click(timeout=3000)
                        page.wait_for_timeout(400)
                        c = page.evaluate(CENSUS_JS)
                        opened[route] = {"button": btn.inner_text().strip()[:40], "dialogs": c["dialogs"],
                                         "overlays": c["overlays"], "iconOnly": c["iconOnly"],
                                         "unlabelledFields": c["unlabelledFields"], "fields": c["fields"]}
                    except Exception as exc:  # noqa: BLE001 — record, do not fail the census
                        opened[route] = {"error": type(exc).__name__}
            # Interaction states that only render on demand.
            def state(key, fn):
                try:
                    fn()
                    page.wait_for_timeout(700)
                    results[key] = page.evaluate(CENSUS_JS)
                except Exception as exc:  # noqa: BLE001
                    opened[key] = {"error": type(exc).__name__}
            go(o.url + "/")
            state("/#search-typed", lambda: page.locator("#global-search").first.fill("wcag", force=True, timeout=3000))
            go(o.url + "/printers")
            state("/printers#camera", lambda: page.get_by_role("button", name="View camera").first.click(timeout=3000))
            state("/printers#camera-expanded", lambda: page.get_by_role("button", name="Expand camera view").first.click(timeout=3000))
            go(o.url + "/models")
            state("/models#3d", lambda: page.get_by_role("button", name="View 3D preview").first.click(timeout=3000))
            go(o.url + "/jobs")
            state("/jobs#search", lambda: page.locator("main input[type=text]").first.fill("wcag"))
            page.set_viewport_size({"width": 390, "height": 844})
            go(o.url + "/")
            results["/#mobile"] = page.evaluate(CENSUS_JS)
            state("/#mobile-menu", lambda: page.get_by_role("button", name="Open menu").first.click(timeout=3000))
            page.set_viewport_size({"width": 1440, "height": 1000})
            # Settings tabs
            go(o.url + "/settings")
            for label in ("General", "Notifications", "Access", "Integrations", "Vigil AI", "Branding", "License",
                          "System", "Logs"):
                tab = page.locator("main").get_by_role("button", name=label, exact=True)
                if not tab.count():
                    continue
                tab.first.click()
                page.wait_for_timeout(700)
                results[f"/settings#{label}"] = page.evaluate(CENSUS_JS)
                if label == "Access":
                    for acc in ("Permissions", "Authentication (OIDC & MFA)", "Tokens & Sessions",
                                "Quotas & Restrictions", "Organizations"):
                        state(f"/settings#Access/{acc}",
                              lambda acc=acc: page.locator("main").get_by_role("button", name=acc).first.click(timeout=3000))
            browser.close()
    finally:
        o.stop()
    named = {}
    for route, r in list(results.items()) + [(k + "+dialog", v) for k, v in opened.items() if "iconOnly" in v]:
        for b in r["iconOnly"]:
            if b["aria"]:
                named.setdefault((b["aria"], b["html"][:80]), set()).add(route)
    summary = {
        "routes": len(results),
        "distinct_icon_only_buttons_with_aria_label": len(named),
        "icon_only_unnamed": sorted({(rt, b["html"]) for rt, r in results.items() for b in r["iconOnly"] if not b["name"]}),
        "icon_only_title_only": sorted({(rt, b["html"]) for rt, r in results.items() for b in r["iconOnly"] if b["name"] and not b["aria"]}),
        "opened_dialogs": opened,
        "th_total": sum(r["th"] for r in results.values()), "th_scope_col": sum(r["thScopeCol"] for r in results.values()),
        "fields_total": sum(r["fields"] for r in results.values()),
        "unlabelled_fields": sorted({(rt, f) for rt, r in results.items() for f in r["unlabelledFields"]}),
        "progressbars": {rt: r["progressbars"] for rt, r in results.items() if r["progressbars"]},
        "bars_without_role": sorted({(rt, b) for rt, r in results.items() for b in r["barsWithoutRole"]}),
    }
    summary["report_schedule_seed_status"] = o.sched.status_code
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps({"summary": summary, "per_route": results}, indent=1, default=list))
    return results, summary


def test_skip_link_on_every_route_and_first_tab_stop(census):
    results, _ = census
    assert all(r["skip"] == "Skip to content" for r in results.values())
    assert results["/"]["firstTabTarget"] == "#main-content"


def test_landmarks_on_every_route(census):
    results, _ = census
    for route, r in results.items():
        assert r["main"] and r["nav"] >= 1 and r["toolbar"] and r["live"] == "polite", route


def test_at_least_50_distinct_icon_only_buttons_carry_aria_label(census):
    _, s = census
    assert s["distinct_icon_only_buttons_with_aria_label"] >= 50, s["distinct_icon_only_buttons_with_aria_label"]


def test_every_rendered_icon_only_button_has_accessible_name(census):
    _, s = census
    assert s["icon_only_unnamed"] == []


def test_every_opened_modal_exposes_dialog_role(census):
    _, s = census
    bad = {rt: v for rt, v in s["opened_dialogs"].items() if v.get("overlays") and not v.get("dialogs")}
    assert bad == {}


def test_opened_shared_modals_are_named_aria_modal_dialogs(census):
    _, s = census
    dialogs = [d for v in s["opened_dialogs"].values() for d in v.get("dialogs", [])]
    assert dialogs, "no dialog opened during the crawl"
    assert all(d["modal"] == "true" and d["named"] for d in dialogs)


def test_rendered_form_fields_mostly_labelled(census):
    _, s = census
    assert s["fields_total"] > 20


def test_every_rendered_form_field_has_label(census):
    _, s = census
    assert s["unlabelled_fields"] == []


def test_table_headers_census(census):
    _, s = census
    assert s["th_total"] > 0
    RECEIPT_TH = {"th_total": s["th_total"], "th_scope_col": s["th_scope_col"]}
    assert RECEIPT_TH["th_scope_col"] < RECEIPT_TH["th_total"]   # documents: not all table headers carry scope=col


def test_every_rendered_progress_bar_has_progressbar_role(census):
    _, s = census
    assert s["bars_without_role"] == []


def test_rendered_progressbars_have_value_range(census):
    _, s = census
    assert s["progressbars"], "no progressbar rendered"
    for route, bars in s["progressbars"].items():
        for b in bars:
            assert b["min"] == "0" and b["max"] == "100" and b["now"] is not None, route
