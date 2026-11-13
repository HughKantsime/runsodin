"""Every backend file that can open a network connection is on a reviewed list.

Spec: .claude/specs/itar-coverage-and-doc-refresh.md (A.4). In 2.1.1 the Square clients used
`requests` directly and nobody noticed that ITAR mode did not cover them. This is a ratchet:
a file that starts importing a network library fails here until someone classifies it.

Classes:
  itar   - can reach an address an administrator or a third party chooses. The file must go
           through ODIN's ITAR guard (asserted below by the guard names it references).
  local  - reaches only go2rtc, or the address of a printer or smart plug the administrator
           registered, or a socket on this machine, or imports the library for types, name
           lookups or exceptions only. The go2rtc address (ODIN_GO2RTC_API_URL) is validated
           at its source: go2rtc_access.go2rtc_api_url() refuses, under ITAR mode, any address
           that is not loopback or on the local network, and the boot audit reports it.
           Printers on the farm's own network are the product's purpose, not an egress path.
"The file references a guard" is a tripwire, not a proof: the behaviour is proven by the
per-client tests (test_itar_mode.py, test_itar_square_and_sso_presets.py and the others).
"""
import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"
# Importing any of these (or a submodule) puts a file on the reviewed list. Database drivers
# are left out on purpose: the database is ODIN's own store, not an egress path.
NETWORK_MODULES = frozenset({
    "requests", "httpx", "urllib.request", "urllib3", "aiohttp", "http.client", "smtplib", "paho",
    "websocket", "websockets", "ftplib", "socket", "ssl", "pywebpush",
})
# asyncio is not a network module, but these calls open a connection without importing one.
NETWORK_CALLS = frozenset({"open_connection", "create_connection", "create_datagram_endpoint"})


def _is_network_module(name):
    return any(name == module or name.startswith(module + ".") for module in NETWORK_MODULES)


def network_imports(source, filename="<source>"):
    """Network modules a Python source uses, found by parsing it (never by matching text).

    Covers `import a, b`, `import a.b as c`, `from a import b`, `from a.b import c`, an
    attribute use such as `urllib.request` after a bare `import urllib`, a string-literal
    `importlib.import_module("a")` / `__import__("a")`, and asyncio's connection calls.
    Relative imports are this project's own modules and are ignored. Raises SyntaxError for
    a file that does not parse.
    """
    found = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Attribute):
            try:
                names = [ast.unparse(node)] if isinstance(node.value, (ast.Name, ast.Attribute)) else []
            except Exception:
                names = []
            if node.attr in NETWORK_CALLS and names and names[0].startswith("asyncio."):
                found.add(names[0])
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if called in ("import_module", "__import__"):
                names = [node.args[0].value]
        found.update(name for name in names if _is_network_module(name))
    return found


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
    "modules/system/troubleshooting.py": "itar",      # refuses a non-local printer address before any request
    "modules/printers/bambu_chamber_camera.py": "itar",  # same check before the camera connection
    "modules/system/routes_health.py": "itar",        # pinned requests; socket is gethostname only
    "core/itar.py": "itar",                           # the guard itself: name lookups only
    # ---- local: go2rtc (address validated at its source, see the docstring), a registered
    # printer, a socket on this machine, or types / name lookups only ----
    "modules/archives/timelapse_capture.py": "local", "modules/inventory/routes/drying_logs.py": "local",
    "modules/inventory/routes/spoolman.py": "local", "modules/printers/camera_routes.py": "local",
    "modules/printers/mjpeg_relay.py": "local", "modules/printers/monitors/mqtt_printer.py": "local",
    "modules/printers/routes_ams.py": "local", "modules/printers/routes_bambu.py": "local",
    "modules/printers/routes_camera_snapshot.py": "local", "modules/printers/telemetry/demo.py": "local",
    "modules/printers/telemetry/live_replay.py": "local", "modules/reporting/routes/analytics.py": "local",
    "modules/vision/detection_thread.py": "local",
    "core/remote_access.py": "local",                 # looks up ODIN_TRUSTED_PROXIES names; opens no connection
    "modules/printers/monitor_commands.py": "local",  # unix socket on this machine
    "modules/system/routes_audit_integrity.py": "local",  # ssl only parses a CA bundle; opens no connection
}


def _network_files():
    found = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith("core/schema/preserved") or "/tests/" in relative or relative.startswith("tests/"):
            continue
        # A file that does not parse raises here and fails the test: it is never skipped.
        if network_imports(path.read_text(encoding="utf-8"), relative):
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


# ---- the detector itself ----

@pytest.mark.parametrize("source, expected", [
    ("import requests", {"requests"}),
    ("import os, requests", {"requests"}),
    ("import os, sys", set()),
    ("import urllib.request as fetch", {"urllib.request"}),
    ("import http.client", {"http.client"}),
    ("import paho.mqtt.client as mqtt", {"paho.mqtt.client"}),
    ("from urllib import request", {"urllib.request"}),
    ("from urllib import parse, request as r", {"urllib.request"}),
    ("from urllib.request import urlopen", {"urllib.request", "urllib.request.urlopen"}),
    ("from http import client", {"http.client"}),
    ("from httpx import AsyncClient", {"httpx", "httpx.AsyncClient"}),
    ("from paho.mqtt import client", {"paho.mqtt", "paho.mqtt.client"}),
    ("def f():\n    import socket\n    return socket", {"socket"}),
    ("try:\n    import websocket\nexcept ImportError:\n    websocket = None", {"websocket"}),
    ("import websockets, aiohttp, urllib3, smtplib, ftplib, ssl, pywebpush",
     {"websockets", "aiohttp", "urllib3", "smtplib", "ftplib", "ssl", "pywebpush"}),
    ("import urllib\nurllib.request.urlopen('http://x')", {"urllib.request", "urllib.request.urlopen"}),
    ("import importlib\nimportlib.import_module('requests')", {"requests"}),
    ("__import__('httpx')", {"httpx"}),
    ("import asyncio\nasync def f():\n    await asyncio.open_connection('h', 1)", {"asyncio.open_connection"}),
])
def test_the_detector_finds_every_import_form(source, expected):
    assert network_imports(source) == expected


@pytest.mark.parametrize("source", [
    "from . import requests",
    "from .httpx import get",
    "from ..socket import socket",
    "import urllib.parse",
    "from urllib import parse",
    "from urllib.parse import urlsplit",
    "import requests_helper, sockets, httpx_extra",
    "from http import HTTPStatus",
    "text = 'import requests'",
    "# import requests",
    "import asyncio\nasyncio.sleep(1)",
])
def test_the_detector_ignores_relative_imports_and_lookalikes(source):
    assert network_imports(source) == set()


def test_a_file_that_does_not_parse_fails_instead_of_being_skipped(tmp_path, monkeypatch):
    with pytest.raises(SyntaxError):
        network_imports("import requests\ndef broken(:\n")
    (tmp_path / "broken.py").write_text("import requests\ndef broken(:\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "BACKEND", tmp_path)
    with pytest.raises(SyntaxError):
        _network_files()
