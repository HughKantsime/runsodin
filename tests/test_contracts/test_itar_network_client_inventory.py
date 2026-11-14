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
           A fixed loopback address is local only while no proxy can carry the request: a
           library that reads HTTP_PROXY from the environment sends a "localhost" request
           to the proxy. Under ITAR mode no ODIN process has a proxy in its environment
           (core.itar.neutralize_environment_proxies, tests/test_contracts/
           test_itar_environment_proxies.py); an entry that depends on that says so.
What puts a file on the list (found by parsing, never by matching text):
  - importing a network library (NETWORK_MODULES), in any import form;
  - asyncio's connection calls, however asyncio is named: `asyncio.open_connection`,
    `import asyncio as aio; aio.open_connection`, `from asyncio import open_connection`,
    and the same call on an event loop object (`loop.create_connection`);
  - starting a program that opens connections itself (NETWORK_PROGRAMS: curl, wget, ffmpeg,
    ffprobe, pg_dump, pg_restore, psql, go2rtc): its name as the first item of an argument
    list, as the command given to subprocess / os.system, or passed by itself to any call.

An itar file must CALL one of the guards. Importing a guard, defining a function with a
guard's name, or mentioning one in a comment or string does not count.

What this test still cannot prove (review 2026-11-13, finding 7). It does no data-flow
analysis, so:
  - it does not show that the guard's result is used, or that the guarded address is the one
    the client connects to: a file that calls is_itar_mode() once and then makes an
    unguarded requests.get(...) elsewhere passes here;
  - it does not see a program whose name is built at run time (a variable, a setting, a
    joined string), a network call reached through getattr or a wrapper in another package,
    or a connection opened by a C extension (OpenCV's ffmpeg) or by a library this list does
    not name;
  - it does not read backend/core/schema/preserved*/ (frozen copies of old releases used
    only to check migrations; they are not imported by the running server) or test folders;
  - SQLAlchemy is not a listed library: every file imports it. The one place a PostgreSQL
    engine is built, and the one function that gives a client program a server, are checked
    in test_itar_postgres_destination.py.
"The file calls a guard" is a tripwire, not a proof: the behaviour is proven by the
per-client tests (test_itar_mode.py, test_itar_square_and_sso_presets.py,
test_itar_local_client_destinations.py, test_itar_device_destinations.py,
test_itar_webhook_single_resolution.py, test_itar_smtp_mqtt_pinning.py,
test_itar_websocket_redirects.py, test_itar_postgres_destination.py and the others).
"""
import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"
# Importing any of these (or a submodule) puts a file on the reviewed list. The PostgreSQL
# drivers are on it: under ITAR mode the database server is a destination like any other.
NETWORK_MODULES = frozenset({
    "requests", "httpx", "urllib.request", "urllib3", "aiohttp", "http.client", "smtplib", "paho",
    "websocket", "websockets", "ftplib", "socket", "ssl", "pywebpush", "psycopg", "psycopg2", "asyncpg",
})
# asyncio is not a network module, but these calls open a connection without importing one.
NETWORK_CALLS = frozenset({"open_connection", "create_connection", "create_datagram_endpoint"})
# Programs that open connections themselves. Starting one puts the file on the reviewed list.
NETWORK_PROGRAMS = frozenset({"curl", "wget", "ffmpeg", "ffprobe", "pg_dump", "pg_dumpall", "pg_restore", "psql",
                              "go2rtc"})
_LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output", "system", "popen",
                        "create_subprocess_exec", "create_subprocess_shell"})


def _is_network_module(name):
    return any(name == module or name.startswith(module + ".") for module in NETWORK_MODULES)


def _program(node, exact=False):
    """The network program a string constant names: its first word (or, with `exact`, the
    whole string), with any directory removed. None when it names none."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    words = [node.value] if exact else node.value.split()
    name = words[0].rsplit("/", 1)[-1] if words else ""
    return name if name in NETWORK_PROGRAMS and " " not in words[0] else None


def _called_name(call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")


def network_programs(source, filename="<source>"):
    """Network programs a Python source starts, as "program:<name>" (see the module docstring
    for the three forms that are found)."""
    found = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        names = []
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            names.append(_program(node.elts[0]))
        elif isinstance(node, ast.Call):
            if _called_name(node) in _LAUNCHERS and node.args:
                names.append(_program(node.args[0]))
            names.extend(_program(value, exact=True)
                         for value in list(node.args) + [keyword.value for keyword in node.keywords])
        found.update(f"program:{name}" for name in names if name)
    return found


def network_imports(source, filename="<source>"):
    """Network modules a Python source uses, found by parsing it (never by matching text).

    Covers `import a, b`, `import a.b as c`, `from a import b`, `from a.b import c`, an
    attribute use such as `urllib.request` after a bare `import urllib`, a string-literal
    `importlib.import_module("a")` / `__import__("a")`, asyncio's connection calls under any
    name for asyncio (and on an event loop object), and the programs network_programs finds.
    Relative imports are this project's own modules and are ignored. Raises SyntaxError for
    a file that does not parse.
    """
    tree = ast.parse(source, filename=filename)
    asyncio_names, module_names = {"asyncio"}, set()
    for node in ast.walk(tree):   # first: what each imported name stands for
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                module_names.add(bound)
                if alias.name == "asyncio" or alias.name.startswith("asyncio."):
                    asyncio_names.add(bound)
    found = network_programs(source, filename)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            if node.module == "asyncio" or node.module.startswith("asyncio."):
                found.update(f"asyncio.{alias.name}" for alias in node.names if alias.name in NETWORK_CALLS)
        elif isinstance(node, ast.Attribute):
            try:
                names = [ast.unparse(node)] if isinstance(node.value, (ast.Name, ast.Attribute)) else []
            except Exception:
                names = []
            if node.attr in NETWORK_CALLS:
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                root_name = root.id if isinstance(root, ast.Name) else None
                # asyncio under any name, or a method of some object (an event loop). A call
                # on another imported module (socket.create_connection) is that module's.
                if root_name in asyncio_names or root_name not in module_names:
                    found.add(f"asyncio.{node.attr}")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            if _called_name(node) in ("import_module", "__import__"):
                names = [node.args[0].value]
        found.update(name for name in names if _is_network_module(name))
    return found


# The guards. A file passes only when its code (never a comment, docstring or other string)
# CALLS one of these names, directly, as an attribute, or under the name it imported it as.
# An import alone, or a function that merely has a guard's name, does not pass.
GUARD_NAMES = frozenset({
    # core.webhook_utils device destinations: ITAR-aware since device_destination_ips checks
    # every resolved address (tests/test_contracts/test_itar_device_destinations.py)
    "device_destination_ips", "device_connect_ip", "device_request", "device_urlopen",
    "pinned_device_destination",
    # core.itar
    "enforce_request_destination", "enforce_host_destination", "pin_for_request",
    "is_private_destination", "is_itar_mode",
    # core.itar_connect: mail and MQTT, looked up once and connected to the checked address
    # (tests/test_contracts/test_itar_smtp_mqtt_pinning.py)
    "smtp_connection", "mqtt_client",
    # built on core.itar: pinned HTTP, the webhook posts, Square, Spoolman, go2rtc
    "pinned_async_request", "safe_post", "trusted_post", "refuse_under_itar",
    "spoolman_request", "go2rtc_api_url", "go2rtc_target",
    # core.itar_database: the PostgreSQL server address, before an engine is built
    # (create_database_engine) or a client program is started (_run_pg_tool)
    # (tests/test_contracts/test_itar_postgres_destination.py)
    "enforce_postgres_destination", "enforce_postgres_command", "_run_pg_tool",
    "pinned_hostaddr", "pinned_client_environment",
})


def guard_names_used(source, filename="<source>"):
    """The guard names a Python source calls, found by parsing it."""
    tree = ast.parse(source, filename=filename)
    aliases = {}   # the name a guard was imported as -> the guard
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in GUARD_NAMES and alias.asname:
                    aliases[alias.asname] = alias.name
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node)
            used.add(aliases.get(name, name) if isinstance(node.func, ast.Name) else name)
    return used & GUARD_NAMES


ITAR_FILES = (
    "core/itar.py", "core/outbound_http.py", "core/webhook_utils.py",   # the guards themselves
    "core/itar_connect.py",                                              # the mail and MQTT guards
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
    "modules/printers/sdcp_websocket.py",   # Elegoo WebSocket: no redirect under ITAR mode (is_itar_mode)
    "modules/printers/adapters/prusalink.py",
    # new in 2.2.0: enforce_request_destination before every request, then prusalink's pinned device_request
    "modules/printers/adapters/octoprint.py", "modules/printers/adapters/ultimaker.py",
    # new in 2.2.0: a discovered address is offered and probed only after is_private_destination
    # and device_destination_ips accept it; the probe runs inside pinned_device_destination
    "modules/printers/moonraker_discovery.py",
    "modules/printers/telemetry/bambu/adapter.py", "modules/printers/telemetry/bambu/ams_acquisition_session.py",
    "modules/printers/telemetry/bambu/commands.py", "modules/printers/telemetry/bambu/ftp_upload.py",
    # go2rtc (ODIN_GO2RTC_API_URL): every address and client setting comes from
    # go2rtc_access.go2rtc_target (tests/test_contracts/test_itar_go2rtc_destinations.py)
    "modules/archives/timelapse_capture.py", "modules/printers/camera_routes.py",
    "modules/printers/mjpeg_relay.py", "modules/printers/routes_camera_snapshot.py",
    "modules/vision/detection_thread.py",
    # go2rtc in its own container: streams outside the checked list are removed at start
    # (tests/test_contracts/test_itar_go2rtc_enterprise_purge.py)
    "modules/printers/go2rtc_purge.py",
    # PostgreSQL client programs (pg_dump, pg_restore): the server address is checked by
    # core.itar_database.enforce_postgres_command inside _run_pg_tool, which is the only
    # function that gives a client a server (tests/test_contracts/test_itar_postgres_destination.py)
    "modules/system/postgres_backup_service.py", "modules/system/postgres_archive_inventory.py",
    "modules/system/postgres_restore_guards.py", "modules/system/postgres_restore_validation.py",
    "modules/system/pre026_restore_stage.py",
)

# local: file -> what it can reach. Re-audited one by one on 2026-11-13, and again the same
# day for "could a proxy from the environment carry it": only mqtt_printer.py could (urllib
# reads the environment). paho reads a proxy only from `mqtt_proxy`, and only when PySocks is
# installed, which ODIN does not ship; a Unix socket and a name lookup have no proxy.
LOCAL_FILES = {
    "core/remote_access.py": "no connection: looks up the names in ODIN_TRUSTED_PROXIES",
    "modules/system/routes_audit_integrity.py": "no connection: ssl only parses a CA bundle",
    "modules/printers/monitor_commands.py": "Unix socket on this machine",
    "modules/printers/mdns_discovery.py":
        "local link only: one UDP query to the fixed mDNS group 224.0.0.251:5353, then it only listens; "
        "the answers are checked by moonraker_discovery before anything is contacted",
    "modules/printers/monitors/mqtt_printer.py":
        "loopback: the fixed address http://localhost:8000 (ODIN's own API), sent with the X-API-Key header. "
        "Local under ITAR mode only because no proxy can carry it there: the request is built with no proxy, "
        "and the process environment holds none. With ITAR mode off it follows HTTP_PROXY like any urllib "
        "request. Printers go through the adapters",
    "modules/printers/telemetry/demo.py":
        "loopback: the demo broker ODIN starts on 127.0.0.1 (paho; no proxy without PySocks, none under ITAR mode)",
    "modules/printers/telemetry/live_replay.py":
        "loopback: the demo broker on 127.0.0.1 (its only callers, demo.py and live_shadow.py, pass that broker; "
        "paho, as demo.py)",
    "modules/inventory/routes/drying_logs.py": "no connection: httpx is imported and not used",
    "modules/inventory/routes/spoolman.py":
        "no connection of its own: httpx is imported and not used; Spoolman goes through core.spoolman_client",
    "modules/printers/routes_ams.py":
        "no connection of its own: httpx is imported and not used; printers go through the adapters",
    "modules/printers/routes_bambu.py": "no connection: httpx is imported and not used",
    "modules/reporting/routes/analytics.py": "no connection: httpx is imported and not used",
    "core/account_lifetime_schema.py":
        "no connection of its own: checks that a connection SQLAlchemy already opened is a psycopg.Connection; "
        "the engine is built, and its server checked under ITAR mode, in core.database_config",
    "core/dispatch_target_lock.py":
        "no connection of its own: reads psycopg's TransactionStatus of a connection SQLAlchemy already opened",
    "core/timelapse_derivative_worker.py":
        "no connection: ffmpeg and ffprobe are given only /proc/self/fd/<n> files and pipes, never a URL",
    "core/timelapse_media_worker.py":
        "no connection: ffmpeg and ffprobe are given only /proc/self/fd/<n> files and pipe:0, never a URL",
    "core/timelapse_probe_worker.py":
        "no connection: ffmpeg is given only a /proc/self/fd/<n> file, never a URL",
    "modules/system/backup_api_export.py":
        "no connection: pg_restore reads an archive file and prints it; it is given no server (no --dbname)",
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
    assert _uses(printer, "urllib.request") == {
        "urllib.request.Request", "urllib.request.urlopen",
        "urllib.request.build_opener", "urllib.request.ProxyHandler"}
    assert printer.count("urllib.request.urlopen(") == 1 and "'http://localhost:8000/api/scheduler/run'" in printer
    # Under ITAR mode the one request is opened with no proxy; the default opener is ITAR-off only.
    assert printer.count("urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5)") == 1
    assert printer.count("urllib.request.Request(") == 1
    replay = (BACKEND / "modules/printers/telemetry/live_replay.py").read_text(encoding="utf-8")
    assert 'def __init__(self, host: str = "127.0.0.1")' in replay
    for caller in ("demo.py", "live_shadow.py", "live_replay.py"):
        source = (BACKEND / "modules/printers/telemetry" / caller).read_text(encoding="utf-8")
        assert "LocalBroker(host" not in source and "LocalBroker(\"" not in source
    # ffmpeg / ffprobe in the timelapse workers: every input and output is a file descriptor
    # path or a pipe, and no argument holds a URL scheme.
    for worker in ("timelapse_derivative_worker.py", "timelapse_media_worker.py", "timelapse_probe_worker.py"):
        source = (BACKEND / "core" / worker).read_text(encoding="utf-8")
        tree = ast.parse(source)
        strings = [node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        assert not [text for text in strings if "://" in text], worker
        inputs = []
        for node in ast.walk(tree):
            if isinstance(node, ast.List):
                for flag, value in zip(node.elts, node.elts[1:]):
                    if isinstance(flag, ast.Constant) and flag.value == "-i":
                        inputs.append(ast.unparse(value))
        assert inputs and all(text == "'pipe:0'" or text.startswith("'/proc/self/fd/' + str(") for text in inputs), \
            (worker, inputs)
    export = (BACKEND / "modules/system/backup_api_export.py").read_text(encoding="utf-8")
    assert "--dbname" not in export and "'-d'" not in export and export.count("subprocess.run(") == 1
    for name in ("core/account_lifetime_schema.py", "core/dispatch_target_lock.py"):
        assert not {use for use in _uses((BACKEND / name).read_text(encoding="utf-8"), "psycopg")
                    if use.split(".")[1] in ("connect", "AsyncConnection", "ConnectionPool")}, name
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
    # review 2026-11-13 finding 7: a guard that is imported, named or defined but never called
    "from core.itar import is_itar_mode\nimport requests\nrequests.get('http://x')",
    "from core.webhook_utils import device_urlopen as urlopen",
    "from core import itar\ncheck = itar.enforce_host_destination",
    "def refuse_under_itar():\n    pass",
    "def safe_post(url):\n    import requests\n    return requests.post(url)",
    "is_itar_mode = True\nprint(is_itar_mode)",
    "guards = {'pin_for_request': None}\nguards['pin_for_request']",
])
def test_a_comment_docstring_string_or_lookalike_is_not_a_guard(source):
    assert guard_names_used(source) == set()


@pytest.mark.parametrize("source, expected", [
    ("from core.webhook_utils import device_connect_ip\ndevice_connect_ip('h', 1)", {"device_connect_ip"}),
    ("from core import webhook_utils\nwebhook_utils.device_request('GET', 'http://h')", {"device_request"}),
    ("def f(host):\n    from core.itar import enforce_host_destination\n    enforce_host_destination(host)",
     {"enforce_host_destination"}),
    ("def refuse_under_itar():\n    pass\nrefuse_under_itar()", {"refuse_under_itar"}),
    ("from core.webhook_utils import device_urlopen as urlopen\nurlopen('http://h')", {"device_urlopen"}),
    ("from core.itar import pin_for_request\nwith pin_for_request('http://h'):\n    pass", {"pin_for_request"}),
    ("async def f(url):\n    return await pinned_async_request('GET', url)", {"pinned_async_request"}),
    ("from modules.system import postgres_backup_service as service\nservice._run_pg_tool(['pg_dump'], 'u', None, '.')",
     {"_run_pg_tool"}),
])
def test_a_guard_called_in_code_is_found(source, expected):
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
    # review 2026-11-13 finding 7: asyncio under another name, and programs that connect
    ("from asyncio import open_connection", {"asyncio.open_connection"}),
    ("from asyncio import open_connection as connect, sleep", {"asyncio.open_connection"}),
    ("import asyncio as aio\nasync def f():\n    await aio.open_connection('h', 1)", {"asyncio.open_connection"}),
    ("import asyncio as aio\naio.get_event_loop().create_connection(None, 'h', 1)", {"asyncio.create_connection"}),
    ("def f(loop):\n    return loop.create_datagram_endpoint(None, remote_addr=('h', 1))",
     {"asyncio.create_datagram_endpoint"}),
    ("import socket\nsocket.create_connection(('h', 1))", {"socket", "socket.create_connection"}),
    ("import subprocess\nsubprocess.run(['curl', '-s', 'http://x'])", {"program:curl"}),
    ("import subprocess\nsubprocess.Popen(('/usr/bin/wget', 'http://x'))", {"program:wget"}),
    ("import subprocess\nsubprocess.check_output('curl -s http://x', shell=True)", {"program:curl"}),
    ("import os\nos.system('wget http://x')", {"program:wget"}),
    ("import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('ffmpeg', '-i', 'rtsp://x')",
     {"program:ffmpeg"}),
    ("command = ['pg_dump', '--dbname', url]", {"program:pg_dump"}),
    ("argv = ['/usr/bin/ffprobe', '-i', source]", {"program:ffprobe"}),
    ("import shutil\nexecutable = shutil.which('pg_restore')", {"program:pg_restore"}),
    ("launch(command('ffmpeg', ['-i', source]))", {"program:ffmpeg"}),
    ("run(['psql', '-c', 'select 1'])\nstart(binary='go2rtc')", {"program:psql", "program:go2rtc"}),
    ("import psycopg", {"psycopg"}),
    ("from psycopg.pq import TransactionStatus", {"psycopg.pq", "psycopg.pq.TransactionStatus"}),
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
    "import asyncio as aio\naio.sleep(1)",
    "from asyncio import sleep, gather",
    "import subprocess\nsubprocess.run(['supervisorctl', 'restart', 'go2rtc'])",   # not started here
    "log.warning('go2rtc refused every ODIN credential')",
    "source = f'ffmpeg:{url}#video=mjpeg'",
    "LOGS = {'go2rtc': '/data/go2rtc.log'}",
    '"""Runs pg_dump and curl."""\n# ffmpeg',
    "import subprocess\nsubprocess.run(['docker', '--version'])",
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
