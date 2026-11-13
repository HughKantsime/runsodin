"""Every backend file that can open a network connection is on a reviewed list.

Spec: .claude/specs/itar-coverage-and-doc-refresh.md (A.4). In 2.1.1 the Square clients used
`requests` directly and nobody noticed that ITAR mode did not cover them. This is a ratchet:
a file that starts importing a network library fails here until someone classifies it.

Classes:
  itar   - can reach an address an administrator, an environment variable or a third party
           chooses: a webhook, mail or MQTT server, Spoolman, an identity provider, Square,
           go2rtc, or a saved printer, camera or smart plug. The file must go through one of
           ODIN's ITAR guards (asserted below by the guard names its code references).
           Printers, cameras and plugs get their address from the device destination
           functions in core/webhook_utils.py (device_destination_ips and the helpers built
           on it), which under ITAR mode refuse any address outside the local network.
  local  - can reach nothing an administrator can point elsewhere: a fixed loopback address,
           a Unix socket, the fixed mDNS multicast group (which routers do not forward),
           or no connection at all (the library is imported for name lookups,
           certificate parsing or exception types, or is not used). Each entry says which.
"The file references a guard" is a tripwire, not a proof: the behaviour is proven by the
per-client tests (test_itar_mode.py, test_itar_square_and_sso_presets.py,
test_itar_local_client_destinations.py, test_itar_device_destinations.py and the others).
"""
import ast
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


# The guards. A file passes only when its code (never a comment, docstring or other string)
# uses one of these names: as a call, an attribute, an import or its own definition.
GUARD_NAMES = frozenset({
    # core.webhook_utils device destinations: ITAR-aware since device_destination_ips checks
    # every resolved address (tests/test_contracts/test_itar_device_destinations.py)
    "device_destination_ips", "device_connect_ip", "device_request", "device_urlopen",
    "pinned_device_destination",
    # core.itar
    "enforce_request_destination", "enforce_host_destination", "pin_for_request",
    "is_private_destination", "is_itar_mode",
    # built on core.itar: pinned HTTP, the webhook posts, Square, Spoolman, go2rtc
    "pinned_async_request", "safe_post", "trusted_post", "refuse_under_itar",
    "spoolman_request", "go2rtc_api_url",
})


def guard_names_used(source, filename="<source>"):
    """The guard names a Python source uses in code, found by parsing it."""
    used = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.name.rsplit(".", 1)[-1])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            used.add(node.name)
    return used & GUARD_NAMES


ITAR_FILES = (
    "core/itar.py", "core/outbound_http.py", "core/webhook_utils.py",   # the guards themselves
    "core/spoolman_client.py", "core/mqtt_power_protocol.py",
    "modules/inventory/services.py",                 # Spoolman, through core.spoolman_client.spoolman_request
    "modules/notifications/alert_dispatcher.py", "modules/notifications/channels.py",
    "modules/notifications/mqtt_republish.py", "modules/notifications/routes/webhooks.py",
    "modules/orders/square_client.py", "modules/orders/square_delivery_client.py",
    "modules/organizations/classroom_service.py", "modules/organizations/education_roster_providers.py",
    "modules/organizations/routes_auth.py", "modules/organizations/routes_users.py",
    "modules/push/apns.py", "modules/reporting/report_runner.py",
    "modules/system/audit_siem_transport.py", "modules/system/routes_health.py",
    "modules/system/troubleshooting.py",
    # saved printers, cameras and smart plugs: the device destination functions
    "modules/system/routes_setup.py", "modules/printers/ledger_display.py",
    "modules/printers/plug_http_protocol.py", "modules/printers/route_utils.py",
    "modules/printers/routes_crud.py", "modules/printers/camera_relay.py",
    "modules/printers/dispatch_readiness.py", "modules/printers/dispatch_bambu_readiness.py",
    "modules/printers/bambu_integration.py", "modules/printers/bambu_chamber_camera.py",
    "modules/printers/adapters/anycubic_lan.py", "modules/printers/adapters/bambu.py",
    "modules/printers/adapters/elegoo.py", "modules/printers/adapters/moonraker.py",
    "modules/printers/adapters/prusalink.py",
    # new in 2.2.0: enforce_request_destination before every request, then prusalink's pinned device_request
    "modules/printers/adapters/octoprint.py", "modules/printers/adapters/ultimaker.py",
    # new in 2.2.0: a discovered address is offered and probed only after is_private_destination
    # and device_destination_ips accept it; the probe runs inside pinned_device_destination
    "modules/printers/moonraker_discovery.py",
    "modules/printers/telemetry/bambu/adapter.py", "modules/printers/telemetry/bambu/ams_acquisition_session.py",
    "modules/printers/telemetry/bambu/commands.py", "modules/printers/telemetry/bambu/ftp_upload.py",
    # go2rtc (ODIN_GO2RTC_API_URL): every address comes from go2rtc_access.go2rtc_api_url
    "modules/archives/timelapse_capture.py", "modules/printers/camera_routes.py",
    "modules/printers/mjpeg_relay.py", "modules/printers/routes_camera_snapshot.py",
    "modules/vision/detection_thread.py",
)

# local: file -> what it can reach. Re-audited one by one on 2026-11-13.
LOCAL_FILES = {
    "core/remote_access.py": "no connection: looks up the names in ODIN_TRUSTED_PROXIES",
    "modules/system/routes_audit_integrity.py": "no connection: ssl only parses a CA bundle",
    "modules/printers/monitor_commands.py": "Unix socket on this machine",
    "modules/printers/mdns_discovery.py":
        "local link only: one UDP query to the fixed mDNS group 224.0.0.251:5353, then it only listens; "
        "the answers are checked by moonraker_discovery before anything is contacted",
    "modules/printers/monitors/mqtt_printer.py":
        "loopback: the fixed address http://localhost:8000 (ODIN's own API); printers go through the adapters",
    "modules/printers/telemetry/demo.py": "loopback: the demo broker ODIN starts on 127.0.0.1",
    "modules/printers/telemetry/live_replay.py":
        "loopback: the demo broker on 127.0.0.1 (its only callers, demo.py and live_shadow.py, pass that broker)",
    "modules/inventory/routes/drying_logs.py": "no connection: httpx is imported and not used",
    "modules/inventory/routes/spoolman.py":
        "no connection of its own: httpx is imported and not used; Spoolman goes through core.spoolman_client",
    "modules/printers/routes_ams.py":
        "no connection of its own: httpx is imported and not used; printers go through the adapters",
    "modules/printers/routes_bambu.py": "no connection: httpx is imported and not used",
    "modules/reporting/routes/analytics.py": "no connection: httpx is imported and not used",
}

REVIEWED = {**{name: "itar" for name in ITAR_FILES}, **{name: "local" for name in LOCAL_FILES}}


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


def test_no_file_is_listed_in_both_classes():
    assert not set(ITAR_FILES) & set(LOCAL_FILES)
    assert len(ITAR_FILES) == len(set(ITAR_FILES))


def test_every_itar_class_file_still_goes_through_the_guard():
    unguarded = sorted(name for name in ITAR_FILES
                       if not guard_names_used((BACKEND / name).read_text(encoding="utf-8"), name))
    assert not unguarded, f"These can reach an administrator-chosen address but no longer use an ITAR guard: {unguarded}"


# ---- the local class: what each file says it cannot reach is checked where it can be ----

def _uses(source, *module_prefixes):
    """Attribute uses such as httpx.get or urllib.request.urlopen (not the import itself)."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, (ast.Name, ast.Attribute)):
            dotted = ast.unparse(node)
            if any(dotted.startswith(prefix + ".") for prefix in module_prefixes):
                found.add(dotted)
    return found


@pytest.mark.parametrize("name", sorted(name for name, why in LOCAL_FILES.items() if "not used" in why))
def test_a_local_file_listed_as_not_using_its_library_really_does_not(name):
    source = (BACKEND / name).read_text(encoding="utf-8")
    assert _uses(source, "httpx", "requests") == set()


def test_the_local_files_with_a_fixed_address_still_have_only_that_address():
    printer = (BACKEND / "modules/printers/monitors/mqtt_printer.py").read_text(encoding="utf-8")
    assert _uses(printer, "urllib.request") == {"urllib.request.Request", "urllib.request.urlopen"}
    assert printer.count("urllib.request.urlopen(") == 1 and "'http://localhost:8000/api/scheduler/run'" in printer
    replay = (BACKEND / "modules/printers/telemetry/live_replay.py").read_text(encoding="utf-8")
    assert 'def __init__(self, host: str = "127.0.0.1")' in replay
    for caller in ("demo.py", "live_shadow.py", "live_replay.py"):
        source = (BACKEND / "modules/printers/telemetry" / caller).read_text(encoding="utf-8")
        assert "LocalBroker(host" not in source and "LocalBroker(\"" not in source
    mdns = (BACKEND / "modules/printers/mdns_discovery.py").read_text(encoding="utf-8")
    assert 'MDNS_GROUP = "224.0.0.251"' in mdns and mdns.count(".sendto(") == 1
    assert "sock.sendto(build_query(), (MDNS_GROUP, MDNS_PORT))" in mdns
    assert ".connect(" not in mdns and "create_connection" not in mdns
    commands = (BACKEND / "modules/printers/monitor_commands.py").read_text(encoding="utf-8")
    assert "AF_INET" not in commands and "socket.AF_UNIX" in commands


# ---- the guard matcher ----

@pytest.mark.parametrize("source", [
    "# goes through the itar guard in webhook_utils\nimport requests\nrequests.get('http://x')",
    '"""Uses enforce_host_destination and device_request."""\nimport requests',
    "import requests\nnote = 'pinned_device_destination'",
    "from core.webhook_utils import WebhookSSRFError, _pin_dns",
    "import itar_helper\nitar_helper.go()",
])
def test_a_comment_docstring_string_or_lookalike_is_not_a_guard(source):
    assert guard_names_used(source) == set()


@pytest.mark.parametrize("source, expected", [
    ("from core.webhook_utils import device_connect_ip\ndevice_connect_ip('h', 1)", {"device_connect_ip"}),
    ("from core import webhook_utils\nwebhook_utils.device_request('GET', 'http://h')", {"device_request"}),
    ("def f(host):\n    from core.itar import enforce_host_destination\n    enforce_host_destination(host)",
     {"enforce_host_destination"}),
    ("def refuse_under_itar():\n    pass", {"refuse_under_itar"}),
    ("from core.webhook_utils import device_urlopen as urlopen", {"device_urlopen"}),
])
def test_a_guard_used_in_code_is_found(source, expected):
    assert guard_names_used(source) == expected


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
