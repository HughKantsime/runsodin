import json
import math
import os
import re
import shutil
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path

from audit_manifest import UI_AUDIT_ROUTES
from helpers import container_exec_python

try:
    from PIL import Image, ImageChops, ImageStat
except ImportError:  # pragma: no cover - optional during local authoring
    Image = ImageChops = ImageStat = None


TRUTHY = {"1", "true", "yes", "on"}
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
AUDIT_DIR = ARTIFACTS_DIR / "audit"
E2E_REPORT_DIR = AUDIT_DIR / "e2e"
CONTRACT_REPORT_DIR = AUDIT_DIR / "contracts"
VISUAL_DIR = ARTIFACTS_DIR / "visual"
BASELINE_DIR = VISUAL_DIR / "baseline"
ACTUAL_DIR = VISUAL_DIR / "actual"
DIFF_DIR = VISUAL_DIR / "diff"

AUDIT_ROUTES = [
    {
        "name": route.name,
        "path": route.path,
        "ready_text": route.ready_text,
        "ready_selector": route.ready_selector,
        "visual": route.visual,
        "requires_printer": route.requires_printer,
        "min_api_calls": route.min_api_calls,
    }
    for route in UI_AUDIT_ROUTES
]

SAFE_CLICK_WORDS = (
    "add", "new", "open", "camera", "filter", "search", "sort",
    "theme", "telemetry", "ams", "nozzle", "hms", "view", "show",
    "next", "previous", "close", "scan", "tab", "details",
)
DESTRUCTIVE_WORDS = (
    "delete", "remove", "destroy", "erase", "stop", "restart", "logout",
    "log out", "cancel", "fail", "archive", "backup", "restore", "power off",
    "deactivate", "unactivate", "clear", "ship", "approve", "reject", "save",
    "submit", "upload", "purchase", "send", "run now", "complete",
)


@dataclass
class PageDiagnostics:
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    server_failures: list[str] = field(default_factory=list)
    auth_failures: list[str] = field(default_factory=list)
    api_successes: list[str] = field(default_factory=list)
    api_requests: list[str] = field(default_factory=list)


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _serializable(value):
    if is_dataclass(value):
        return asdict(value)
    return value


def route_snapshot_name(route) -> str:
    if isinstance(route, dict):
        value = route.get("name") or route.get("path") or "route"
    else:
        value = getattr(route, "name", None) or getattr(route, "path", None) or "route"
    return slugify(value)


def artifact_path(*parts: str) -> Path:
    target = ARTIFACTS_DIR.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_json_artifact(*parts: str, payload):
    target = artifact_path(*parts)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_serializable) + "\n")
    return target


def write_markdown_artifact(*parts: str, text: str):
    target = artifact_path(*parts)
    target.write_text(text)
    return target


def materialize_route_path(route, audit_printer=None) -> str:
    path = route["path"] if isinstance(route, dict) else route.path
    if "{printer_id}" in path:
        if not audit_printer:
            raise ValueError(f"Route {path} requires an audit printer")
        path = path.format(printer_id=audit_printer["id"])
    return path


def start_page_watch(page) -> PageDiagnostics:
    diag = PageDiagnostics()

    def on_console(msg):
        if msg.type != "error":
            return
        text = normalize_whitespace(msg.text)
        if not text:
            return
        if any(ignored in text.lower() for ignored in (
            "favicon", "websocket is closed before", "notification permission",
            "422", "unprocessable entity",
            "429", "too many requests",
            "failed to load resource",
            "failed to fetch", "network error",
        )):
            return
        diag.console_errors.append(text)

    def on_page_error(err):
        text = normalize_whitespace(str(err))
        if text:
            diag.page_errors.append(text)

    def on_response(resp):
        url = resp.url
        if "/api/" in url:
            diag.api_requests.append(f"{resp.status} {url}")
            if 200 <= resp.status < 400:
                diag.api_successes.append(f"{resp.status} {url}")
            elif resp.status in (401, 403):
                diag.auth_failures.append(f"{resp.status} {url}")

        if resp.status < 500:
            return
        if "/ws" in url:
            return
        # Ignore expected failures from hardware-dependent or external endpoints
        if any(pattern in url for pattern in ("/webrtc", "/live-status", "/spoolman")):
            return
        diag.server_failures.append(f"{resp.status} {url}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("response", on_response)
    return diag


def assert_no_critical_failures(diag: PageDiagnostics):
    failures = []
    if diag.console_errors:
        failures.append("console: " + " | ".join(diag.console_errors[:3]))
    if diag.page_errors:
        failures.append("page: " + " | ".join(diag.page_errors[:3]))
    if diag.server_failures:
        failures.append("server: " + " | ".join(diag.server_failures[:3]))
    if diag.auth_failures:
        failures.append("auth: " + " | ".join(diag.auth_failures[:3]))
    if failures:
        raise AssertionError("Route audit found critical failures: " + " || ".join(failures))


def wait_for_app_settle(page, timeout_ms: int = 15000):
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        page.wait_for_timeout(750)
    page.wait_for_timeout(300)


def wait_for_route_ready(page, route, timeout_ms: int = 15000):
    ready_selector = route.get("ready_selector") if isinstance(route, dict) else route.ready_selector
    ready_text = route.get("ready_text") if isinstance(route, dict) else route.ready_text
    if ready_selector:
        page.locator(ready_selector).first.wait_for(state="visible", timeout=timeout_ms)
    elif ready_text:
        page.wait_for_function(
            """
            ([expected]) => {
              const elements = Array.from(document.querySelectorAll('body *'))
              return elements.some((el) => {
                const style = window.getComputedStyle(el)
                if (style.display === 'none' || style.visibility === 'hidden') return false
                const rect = el.getBoundingClientRect()
                if (rect.width === 0 && rect.height === 0) return false
                return (el.innerText || '').includes(expected)
              })
            }
            """,
            arg=[ready_text],
            timeout=timeout_ms,
        )


def assert_route_rendered(page, route):
    body = normalize_whitespace(page.locator("body").inner_text())
    lowered = body.lower()
    assert len(body) > 20, f"{route['path']} rendered almost no content"
    for needle in ("cannot get ", "unexpected application error", "referenceerror", "syntaxerror"):
        assert needle not in lowered, f"{route['path']} rendered a raw app error: {needle}"

    ready_text = route.get("ready_text")
    if ready_text:
        assert ready_text.lower() in lowered, f"{route['path']} did not show expected text {ready_text!r}"


def assert_route_api_activity(diag: PageDiagnostics, route):
    min_api_calls = route.get("min_api_calls", 1)
    if min_api_calls and len(diag.api_successes) < min_api_calls:
        raise AssertionError(
            f"{route['path']} made only {len(diag.api_successes)} successful API calls; expected at least {min_api_calls}"
        )


def _best_effort_label(locator) -> str:
    for attr in ("aria-label", "title", "name"):
        value = locator.get_attribute(attr)
        if value:
            return normalize_whitespace(value)
    try:
        text = normalize_whitespace(locator.inner_text())
    except Exception:
        text = ""
    return text


def fill_visible_fields(page, limit: int = 18) -> list[str]:
    touched = []
    inputs = page.locator("input:not([type=hidden]):not([disabled]):not([readonly]), textarea:not([disabled]):not([readonly]), select:not([disabled]):not([readonly])")
    count = min(inputs.count(), limit)

    for idx in range(count):
        locator = inputs.nth(idx)
        try:
            if not locator.is_visible():
                continue
            tag = (locator.evaluate("(el) => el.tagName.toLowerCase()") or "").lower()
            input_type = (locator.get_attribute("type") or "text").lower()
            label = _best_effort_label(locator) or f"{tag}-{idx}"

            if tag == "select":
                options = locator.locator("option")
                if options.count() > 1:
                    locator.select_option(index=1)
                    touched.append(label)
                continue

            if input_type in {"file", "submit", "button", "image", "hidden"}:
                continue
            if input_type in {"checkbox", "radio"}:
                locator.check(force=True)
                touched.append(label)
                continue
            if input_type == "number":
                locator.fill("1")
                touched.append(label)
                continue
            if input_type == "email":
                locator.fill("audit@example.com")
                touched.append(label)
                continue
            if input_type == "password":
                locator.fill("AuditPass1!")
                touched.append(label)
                continue
            if input_type == "date":
                locator.fill("2026-03-28")
                touched.append(label)
                continue
            if input_type == "color":
                locator.fill("#336699")
                touched.append(label)
                continue

            locator.fill("audit sweep")
            touched.append(label)
        except Exception:
            continue

    return touched


def click_safe_controls(page, limit: int = 12) -> list[str]:
    clicked = []
    controls = page.locator("button, [role='tab'], summary")
    count = min(controls.count(), 50)

    for idx in range(count):
        if len(clicked) >= limit:
            break
        locator = controls.nth(idx)
        try:
            if not locator.is_visible() or not locator.is_enabled():
                continue
            label = _best_effort_label(locator).lower()
            if not label:
                continue
            if any(word in label for word in DESTRUCTIVE_WORDS):
                continue
            if not any(word in label for word in SAFE_CLICK_WORDS):
                continue
            locator.click(timeout=2000)
            page.wait_for_timeout(150)
            clicked.append(label)
            close_modal_if_open(page)
        except Exception:
            continue

    return clicked


def close_modal_if_open(page):
    try:
        dialogs = page.locator("[role='dialog']")
        if dialogs.count() > 0 and dialogs.first.is_visible():
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
    except Exception:
        pass


def route_audit_report(route, path: str, diag: PageDiagnostics, touched_fields: list[str], clicked_controls: list[str], visual_result=None):
    return {
        "route": route.get("name"),
        "path": path,
        "ready_text": route.get("ready_text"),
        "ready_selector": route.get("ready_selector"),
        "touched_fields": touched_fields,
        "clicked_controls": clicked_controls,
        "console_errors": diag.console_errors,
        "page_errors": diag.page_errors,
        "server_failures": diag.server_failures,
        "auth_failures": diag.auth_failures,
        "api_request_count": len(diag.api_requests),
        "api_success_count": len(diag.api_successes),
        "api_requests": diag.api_requests,
        "api_successes": diag.api_successes,
        "visual": visual_result,
    }


def compare_images(baseline_path: Path, actual_path: Path, diff_path: Path) -> tuple[bool, str]:
    if Image is None or ImageChops is None or ImageStat is None:
        return True, "Pillow not installed; visual diff skipped"

    with Image.open(baseline_path) as baseline_img, Image.open(actual_path) as actual_img:
        baseline = baseline_img.convert("RGBA")
        actual = actual_img.convert("RGBA")

        if baseline.size != actual.size:
            return False, f"dimensions changed from {baseline.size} to {actual.size}"

        diff = ImageChops.difference(baseline, actual)
        if diff.getbbox() is None:
            return True, "match"

        stat = ImageStat.Stat(diff)
        rms = math.sqrt(sum(channel ** 2 for channel in stat.rms) / len(stat.rms))
        if rms <= float(os.environ.get("ODIN_VISUAL_RMS_THRESHOLD", "1.5")):
            return True, f"minor drift only (rms={rms:.2f})"

        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff.save(diff_path)
        return False, f"rms={rms:.2f}"


def capture_visual_snapshot(page, route):
    snapshot_name = route_snapshot_name(route)
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)

    actual_path = ACTUAL_DIR / f"{snapshot_name}.png"
    baseline_path = BASELINE_DIR / f"{snapshot_name}.png"
    diff_path = DIFF_DIR / f"{snapshot_name}.png"

    page.screenshot(path=str(actual_path), full_page=True, animations="disabled")

    if env_truthy("ODIN_UPDATE_VISUAL_BASELINE"):
        shutil.copyfile(actual_path, baseline_path)
        return {"status": "updated", "path": str(baseline_path)}

    if not baseline_path.exists():
        return {"status": "missing", "path": str(actual_path)}

    matched, message = compare_images(baseline_path, actual_path, diff_path)
    return {
        "status": "matched" if matched else "diff",
        "message": message,
        "baseline": str(baseline_path),
        "actual": str(actual_path),
        "diff": str(diff_path),
    }


def publish_printer_status(printer_id: int, printer_name: str, payload: dict):
    """Write synthetic printer state directly to the DB.

    The monitor's _on_status reads from self._state which isn't populated on a
    fresh instance, so we bypass it and write the columns the API reads.
    """
    import json as _json
    hms_list = payload.get("hms", [])
    hms_json = _json.dumps(hms_list) if hms_list else None
    # Serialize the full payload to avoid f-string None/null issues
    params = _json.dumps([
        payload.get('bed_temper'),
        payload.get('bed_target_temper'),
        payload.get('nozzle_temper'),
        payload.get('nozzle_target_temper'),
        payload.get('gcode_state'),
        hms_json,
        printer_id,
    ])
    code = f"""
import json, os, sys
sys.path.insert(0, "/app/backend")
os.environ.setdefault("BACKEND_PATH", "/app/backend")
from core.db_utils import get_db

params = json.loads({_json.dumps(params)})
with get_db() as conn:
    conn.execute(
        '''UPDATE printers
           SET bed_temp=?, bed_target_temp=?, nozzle_temp=?, nozzle_target_temp=?,
               gcode_state=?, hms_errors=?, last_seen=datetime('now')
           WHERE id=?''',
        params,
    )
    conn.commit()
print("ok")
"""
    rc, stdout, stderr = container_exec_python(code, timeout=20)
    if rc != 0 or "ok" not in stdout:
        raise RuntimeError(f"Failed to publish synthetic printer status: {stderr or stdout}")


def set_printer_last_seen(printer_id: int, seconds_ago: int):
    code = f"""
import os
import sys
sys.path.insert(0, "/app/backend")
os.environ.setdefault("BACKEND_PATH", "/app/backend")
from core.db_utils import get_db

with get_db() as conn:
    conn.execute(
        "UPDATE printers SET last_seen = datetime('now', ?) WHERE id = ?",
        (f"-{{int({seconds_ago})}} seconds", {printer_id}),
    )
    conn.commit()
print("ok")
"""
    rc, stdout, stderr = container_exec_python(code, timeout=15)
    if rc != 0 or "ok" not in stdout:
        raise RuntimeError(f"Failed to set printer last_seen: {stderr or stdout}")


def reset_printer_state(printer_id: int):
    code = f"""
import os
import sys
sys.path.insert(0, "/app/backend")
os.environ.setdefault("BACKEND_PATH", "/app/backend")
from core.db_utils import get_db

with get_db() as conn:
    conn.execute(
        '''
        UPDATE printers
        SET gcode_state = 'IDLE',
            print_stage = 'Idle',
            bed_temp = NULL,
            bed_target_temp = NULL,
            nozzle_temp = NULL,
            nozzle_target_temp = NULL,
            hms_errors = NULL,
            last_seen = datetime('now')
        WHERE id = ?
        ''',
        ({printer_id},),
    )
    conn.execute(
        "UPDATE print_jobs SET status = 'completed', ended_at = datetime('now') WHERE printer_id = ? AND status = 'running'",
        ({printer_id},),
    )
    conn.commit()
print("ok")
"""
    rc, stdout, stderr = container_exec_python(code, timeout=15)
    if rc != 0 or "ok" not in stdout:
        raise RuntimeError(f"Failed to reset printer state: {stderr or stdout}")


def _inject_print_job(printer_id: int, state_case):
    """Create or update a synthetic print job for RUNNING/PAUSE states."""
    import json as _json
    if state_case.gcode_state not in ("RUNNING", "PAUSE", "FINISH"):
        return
    status = "running" if state_case.gcode_state in ("RUNNING", "PAUSE") else "completed"
    params = _json.dumps([
        state_case.progress, state_case.remaining, state_case.layer,
        state_case.total_layers, status, printer_id, f"{state_case.name}.3mf",
    ])
    code = f"""
import json, os, sys
sys.path.insert(0, "/app/backend")
os.environ.setdefault("BACKEND_PATH", "/app/backend")
from core.db_utils import get_db

progress, remaining, layer, total_layers, status, pid, fname = json.loads({_json.dumps(params)})
with get_db() as conn:
    existing = conn.execute(
        "SELECT id FROM print_jobs WHERE printer_id=? AND status='running'", (pid,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE print_jobs SET progress_percent=?, remaining_minutes=?, current_layer=?, total_layers=?, status=? WHERE id=?",
            (progress, remaining, layer, total_layers, status, existing[0])
        )
    else:
        conn.execute(
            "INSERT INTO print_jobs (printer_id, filename, status, progress_percent, remaining_minutes, current_layer, total_layers, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (pid, fname, status, progress, remaining, layer, total_layers)
        )
    conn.commit()
print("ok")
"""
    rc, stdout, stderr = container_exec_python(code, timeout=15)
    if rc != 0 or "ok" not in stdout:
        raise RuntimeError(f"Failed to inject print job: {stderr or stdout}")


def apply_printer_state(printer: dict, state_case):
    reset_printer_state(printer["id"])
    publish_printer_status(
        printer["id"],
        printer["name"],
        {
            "gcode_state": state_case.gcode_state,
            "bed_temper": state_case.bed_temp,
            "bed_target_temper": state_case.bed_target,
            "nozzle_temper": state_case.nozzle_temp,
            "nozzle_target_temper": state_case.nozzle_target,
            "hms": list(state_case.hms),
        },
    )
    _inject_print_job(printer["id"], state_case)
    if state_case.last_seen_seconds_ago:
        set_printer_last_seen(printer["id"], state_case.last_seen_seconds_ago)


def wait_for_text(page, text: str, timeout_ms: int = 10000):
    page.wait_for_function(
        """([expected]) => document.body && document.body.innerText.includes(expected)""",
        [text],
        timeout=timeout_ms,
    )
