"""Every backend file that can open a network connection is on a reviewed list.

Spec: .claude/specs/itar-coverage-and-doc-refresh.md (A.4). In 2.1.1 the Square clients used
`requests` directly and nobody noticed that ITAR mode did not cover them. This is a ratchet:
a file that starts importing a network library fails here until someone classifies it.

Classes:
  itar   - can reach an address an administrator or a third party chooses. The file must go
           through ODIN's ITAR guard (asserted below by the guard names it references).
  local  - reaches only this machine's go2rtc, or the address of a printer or smart plug the
           administrator registered, or imports the library for types/exceptions only.
           Printers on the farm's own network are the product's purpose, not an egress path.
"The file references a guard" is a tripwire, not a proof: the behaviour is proven by the
per-client tests (test_itar_mode.py, test_itar_square_and_sso_presets.py and the others).
"""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
NETWORK_IMPORT = re.compile(
    r"^\s*(?:import|from)\s+(requests|httpx|urllib\.request|aiohttp|http\.client|smtplib|paho|websockets?|ftplib|urllib3)\b",
    re.MULTILINE)
GUARD = re.compile(r"itar|outbound_http|pinned_|enforce_(?:request|host)_destination|pin_for_request|webhook_utils|refuse_under_itar")

REVIEWED = {
    # ---- itar: must reference the guard ----
    "core/outbound_http.py": "itar", "core/webhook_utils.py": "itar", "core/spoolman_client.py": "itar",
    "core/mqtt_power_protocol.py": "itar",
    "modules/inventory/services.py": "itar",
    "modules/notifications/alert_dispatcher.py": "itar", "modules/notifications/channels.py": "itar",
    "modules/notifications/mqtt_republish.py": "itar", "modules/notifications/routes/webhooks.py": "itar",
    "modules/orders/square_client.py": "itar", "modules/orders/square_delivery_client.py": "itar",
    "modules/organizations/classroom_service.py": "itar", "modules/organizations/education_roster_providers.py": "itar",
    "modules/organizations/routes_auth.py": "itar", "modules/organizations/routes_users.py": "itar",
    "modules/push/apns.py": "itar", "modules/reporting/report_runner.py": "itar",
    "modules/system/audit_siem_transport.py": "itar", "modules/system/routes_setup.py": "itar",
    "modules/printers/ledger_display.py": "itar", "modules/printers/plug_http_protocol.py": "itar",
    "modules/printers/route_utils.py": "itar", "modules/printers/routes_crud.py": "itar",
    "modules/printers/camera_relay.py": "itar", "modules/printers/dispatch_readiness.py": "itar",
    "modules/printers/dispatch_bambu_readiness.py": "itar", "modules/printers/bambu_integration.py": "itar",
    "modules/printers/adapters/anycubic_lan.py": "itar", "modules/printers/adapters/bambu.py": "itar",
    "modules/printers/adapters/elegoo.py": "itar", "modules/printers/adapters/moonraker.py": "itar",
    "modules/printers/adapters/prusalink.py": "itar",
    "modules/printers/adapters/octoprint.py": "itar", "modules/printers/adapters/ultimaker.py": "itar",
    "modules/printers/telemetry/bambu/adapter.py": "itar", "modules/printers/telemetry/bambu/ams_acquisition_session.py": "itar",
    "modules/printers/telemetry/bambu/commands.py": "itar", "modules/printers/telemetry/bambu/ftp_upload.py": "itar",
    # ---- local: go2rtc on this machine, a registered printer, or types only ----
    "modules/archives/timelapse_capture.py": "local", "modules/inventory/routes/drying_logs.py": "local",
    "modules/inventory/routes/spoolman.py": "local", "modules/printers/camera_routes.py": "local",
    "modules/printers/mjpeg_relay.py": "local", "modules/printers/monitors/mqtt_printer.py": "local",
    "modules/printers/routes_ams.py": "local", "modules/printers/routes_bambu.py": "local",
    "modules/printers/routes_camera_snapshot.py": "local", "modules/printers/telemetry/demo.py": "local",
    "modules/printers/telemetry/live_replay.py": "local", "modules/reporting/routes/analytics.py": "local",
    "modules/system/troubleshooting.py": "local", "modules/vision/detection_thread.py": "local",
}


def _network_files():
    found = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith("core/schema/preserved") or "/tests/" in relative or relative.startswith("tests/"):
            continue
        if NETWORK_IMPORT.search(path.read_text(encoding="utf-8", errors="replace")):
            found.add(relative)
    return found


def test_every_file_that_imports_a_network_library_has_been_classified():
    found = _network_files()
    new = sorted(found - set(REVIEWED))
    gone = sorted(set(REVIEWED) - found)
    assert not new, ("New network client(s): decide whether ITAR mode must cover each, guard it if so, "
                     f"then add it to REVIEWED with its class: {new}")
    assert not gone, f"No longer import a network library; remove from REVIEWED: {gone}"


def test_every_itar_class_file_still_goes_through_the_guard():
    unguarded = sorted(name for name, kind in REVIEWED.items()
                       if kind == "itar" and not GUARD.search((BACKEND / name).read_text(encoding="utf-8", errors="replace")))
    assert not unguarded, f"These can reach an administrator-chosen address but no longer reference the ITAR guard: {unguarded}"
