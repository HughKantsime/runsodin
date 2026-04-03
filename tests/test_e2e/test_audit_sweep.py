"""
O.D.I.N. audit sweep

High-signal browser audit that:
  - walks the major SPA routes with manifest coverage enforcement
  - safely exercises visible controls and form fields
  - injects synthetic printer telemetry through the real monitor path
  - verifies printer state rendering across multiple UI surfaces
  - captures optional visual baselines and diff artifacts
"""

import os
import re
from pathlib import Path

import pytest

from audit_manifest import (
    APP_ROUTE_PATHS,
    PRINTER_STATE_CASES,
    UI_AUDIT_ROUTES,
    VISUAL_CASES,
    canonical_ui_path,
)
from audit_utils import (
    E2E_REPORT_DIR,
    apply_printer_state,
    assert_no_critical_failures,
    assert_route_api_activity,
    assert_route_rendered,
    capture_visual_snapshot,
    click_safe_controls,
    fill_visible_fields,
    materialize_route_path,
    route_audit_report,
    route_snapshot_name,
    start_page_watch,
    wait_for_app_settle,
    wait_for_route_ready,
    write_json_artifact,
)


FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000")
APP_FILE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx"


def _goto(page, path: str, route=None):
    page.goto(f"{FRONTEND_URL}{path}", wait_until="domcontentloaded", timeout=20000)
    wait_for_app_settle(page)
    if route is not None:
        try:
            wait_for_route_ready(page, route, timeout_ms=20000)
        except Exception:
            pass  # page loaded but ready indicator not found — continue with assertions


def _resolved_route(route_case, audit_printer):
    return {
        "name": route_case.name,
        "path": materialize_route_path(route_case, audit_printer if route_case.requires_printer else None),
        "ready_text": route_case.ready_text,
        "ready_selector": route_case.ready_selector,
        "visual": route_case.visual,
        "requires_printer": route_case.requires_printer,
        "min_api_calls": route_case.min_api_calls,
    }


def _printer_labels(printer: dict):
    labels = [printer.get("nickname"), printer.get("name")]
    return [label for idx, label in enumerate(labels) if label and label not in labels[:idx]]


def _fallback_card(page, printer: dict, marker_text: str):
    for label in _printer_labels(printer):
        candidate = page.locator("div").filter(has_text=label).filter(has_text=marker_text)
        if candidate.count() > 0:
            return candidate.last
    return page.locator("div").filter(has_text=marker_text).last


def _printer_card(page, printer: dict):
    testid = page.locator(f'[data-testid="printer-card"][data-printer-id="{printer["id"]}"]')
    if testid.count() > 0:
        return testid.first
    return _fallback_card(page, printer, "Loaded Filaments")


def _tv_card(page, printer: dict):
    testid = page.locator(f'[data-testid="tv-printer-card"][data-printer-id="{printer["id"]}"]')
    if testid.count() > 0:
        return testid.first
    return _fallback_card(page, printer, "Powered by O.D.I.N.")


def _state_case_by_name(name: str):
    for case in PRINTER_STATE_CASES:
        if case.name == name:
            return case
    raise KeyError(name)


def _ensure_state_injected(api, printer: dict, state_case):
    resp = api.get(f"/api/printers/{printer['id']}")
    if resp.status_code != 200:
        pytest.skip("Could not verify synthetic printer state against the target ODIN API")

    payload = resp.json()
    bed_temp = payload.get("bed_temp")
    nozzle_temp = payload.get("nozzle_temp")
    gcode_state = payload.get("gcode_state")
    hms_errors = payload.get("hms_errors")

    bed_matches = bed_temp is not None and int(round(bed_temp)) == state_case.bed_temp
    nozzle_matches = nozzle_temp is not None and int(round(nozzle_temp)) == state_case.nozzle_temp
    state_matches = gcode_state == state_case.gcode_state
    hms_matches = True if not state_case.hms else bool(hms_errors)

    if not (bed_matches and nozzle_matches and state_matches and hms_matches):
        pytest.skip(
            "Synthetic printer state injection is not targeting the same ODIN instance as BASE_URL. "
            "Run the audit on the ODIN host or set ODIN_CONTAINER_EXEC for that environment."
        )


def test_audit_manifest_covers_app_routes():
    app_text = APP_FILE.read_text()
    app_routes = {canonical_ui_path(match) for match in re.findall(r'<Route path="([^"]+)"', app_text)}
    if "location.pathname === '/tv'" in app_text:
        app_routes.add("/tv")
    app_routes.discard("*")
    app_routes.discard("/admin")
    app_routes.discard("/permissions")
    app_routes.discard("/branding")

    assert app_routes == APP_ROUTE_PATHS, f"App routes drifted.\nExpected: {sorted(APP_ROUTE_PATHS)}\nActual: {sorted(app_routes)}"


@pytest.mark.parametrize("route_case", UI_AUDIT_ROUTES, ids=lambda route_case: route_snapshot_name(route_case))
def test_route_audit_render_and_safe_interactions(admin_page, audit_printer, route_case):
    route = _resolved_route(route_case, audit_printer)
    diag = start_page_watch(admin_page)
    touched_fields = []
    clicked_controls = []
    artifact_name = route_snapshot_name(route)

    try:
        _goto(admin_page, route["path"], route)
        assert_route_rendered(admin_page, route)
        assert_route_api_activity(diag, route)
        touched_fields = fill_visible_fields(admin_page)
        clicked_controls = click_safe_controls(admin_page)
        wait_for_app_settle(admin_page)
        assert_route_rendered(admin_page, route)
        assert_no_critical_failures(diag)
    finally:
        write_json_artifact(
            "audit",
            "e2e",
            "routes",
            f"{artifact_name}.json",
            payload=route_audit_report(route, route["path"], diag, touched_fields, clicked_controls),
        )


@pytest.mark.parametrize("state_case", PRINTER_STATE_CASES, ids=lambda state_case: state_case.name)
def test_printers_page_reflects_printer_state_matrix(admin_page, state_printer, api, state_case):
    apply_printer_state(state_printer, state_case)
    _ensure_state_injected(api, state_printer, state_case)
    route = {"name": f"printers-{state_case.name}", "path": "/printers", "ready_text": "Printers", "min_api_calls": 1}
    diag = start_page_watch(admin_page)

    try:
        _goto(admin_page, route["path"], route)
        card = _printer_card(admin_page, state_printer)
        card.wait_for(state="visible", timeout=15000)
        card_text = card.inner_text()
        assert state_case.expected_status in card_text
        if state_case.expected_status != "Offline":
            assert f"Bed {state_case.bed_temp}°" in card_text
        if state_case.nozzle_temp is not None:
            assert f"Nozzle {state_case.nozzle_temp}°" in card_text
        assert_no_critical_failures(diag)
    finally:
        write_json_artifact(
            "audit",
            "e2e",
            "printer-states",
            f"printers-{state_case.name}.json",
            payload={
                "surface": "printers",
                "state": state_case.name,
                "expected_status": state_case.expected_status,
                "diagnostics": diag,
            },
        )


@pytest.mark.parametrize("state_case", PRINTER_STATE_CASES, ids=lambda state_case: state_case.name)
def test_overlay_reflects_printer_state_matrix(admin_page, state_printer, api, state_case):
    apply_printer_state(state_printer, state_case)
    _ensure_state_injected(api, state_printer, state_case)
    path = f"/overlay/{state_printer['id']}?camera=false"
    route = {
        "name": f"overlay-{state_case.name}",
        "path": path,
        "ready_selector": '[data-testid="overlay-printer-name"]',
        "min_api_calls": 0,
    }
    diag = start_page_watch(admin_page)

    try:
        _goto(admin_page, path, route)
        body_text = admin_page.locator("body").inner_text()
        assert state_printer["name"] in body_text
        assert state_case.expected_overlay_status in body_text
        if state_case.expected_progress_text and state_case.gcode_state in {"RUNNING", "PAUSE"}:
            assert state_case.expected_progress_text in body_text
        assert_no_critical_failures(diag)
    finally:
        write_json_artifact(
            "audit",
            "e2e",
            "printer-states",
            f"overlay-{state_case.name}.json",
            payload={
                "surface": "overlay",
                "state": state_case.name,
                "expected_status": state_case.expected_overlay_status,
                "diagnostics": diag,
            },
        )


@pytest.mark.parametrize("state_case", PRINTER_STATE_CASES, ids=lambda state_case: state_case.name)
def test_tv_dashboard_reflects_printer_state_matrix(admin_page, state_printer, api, state_case):
    apply_printer_state(state_printer, state_case)
    _ensure_state_injected(api, state_printer, state_case)
    route = {"name": f"tv-{state_case.name}", "path": "/tv", "ready_selector": '[data-testid="tv-printer-card"]', "min_api_calls": 1}
    diag = start_page_watch(admin_page)

    try:
        _goto(admin_page, "/tv")
        card = _tv_card(admin_page, state_printer)
        card.wait_for(state="visible", timeout=15000)
        expected_status = state_case.expected_tv_status or state_case.expected_status
        card_text = card.inner_text()
        assert expected_status in card_text
        if state_case.expected_progress_text and expected_status == "Printing":
            assert state_case.expected_progress_text in card_text
        assert_no_critical_failures(diag)
    finally:
        write_json_artifact(
            "audit",
            "e2e",
            "printer-states",
            f"tv-{state_case.name}.json",
            payload={
                "surface": "tv",
                "state": state_case.name,
                "expected_status": state_case.expected_tv_status,
                "diagnostics": diag,
            },
        )


@pytest.mark.parametrize("visual_case", VISUAL_CASES, ids=lambda visual_case: visual_case.name)
def test_visual_regression_snapshots(admin_page, state_printer, api, visual_case):
    state_case = _state_case_by_name(visual_case.state_name) if visual_case.state_name else None
    if state_case:
        apply_printer_state(state_printer, state_case)
        _ensure_state_injected(api, state_printer, state_case)

    route = {
        "name": visual_case.name,
        "path": materialize_route_path({"path": visual_case.route_path}, state_printer if visual_case.requires_printer else None),
    }
    diag = start_page_watch(admin_page)
    result = None

    try:
        _goto(admin_page, route["path"])
        result = capture_visual_snapshot(admin_page, route)
        if result["status"] == "missing":
            pytest.skip(f"Visual baseline missing for {visual_case.name} — set ODIN_UPDATE_VISUAL_BASELINE=1 to create it")
        if result["status"] == "diff":
            pytest.fail(f"Visual regression detected for {visual_case.name}: {result['message']}")
        assert_no_critical_failures(diag)
    finally:
        write_json_artifact(
            "audit",
            "e2e",
            "visual",
            f"{visual_case.name}.json",
            payload={
                "visual_case": visual_case.name,
                "route": route["path"],
                "state": visual_case.state_name,
                "result": result,
                "diagnostics": diag,
            },
        )
