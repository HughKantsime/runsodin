"""Under ITAR mode no ODIN process uses a proxy taken from the environment.

Found by review 2026-11-13, twice in one day:
1. modules/printers/monitors/mqtt_printer.py posted to ODIN's own API on localhost, with the
   X-API-Key header, through HTTP_PROXY (urllib's default opener reads the environment and
   does not exempt localhost).
2. modules/printers/adapters/elegoo.py connected the SDCP WebSocket through HTTP_PROXY
   (websocket-client reads http_proxy itself) although the printer address had been checked.

Guarding client by client kept missing one, so the fix is at the root:
core.itar.neutralize_environment_proxies removes every proxy variable from the process
environment under ITAR mode. It runs when the `core` or `modules` package is first imported
and in docker/entrypoint.sh. These tests prove it for each library ODIN uses, check that
every way an ODIN process starts reaches it first, and check that nothing changes with ITAR
mode off.

Every transport here is a fake. Nothing opens a connection.
"""
import ast
import ctypes
import os
import re
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests

from core import itar

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PROXY_HOST = "203.0.113.9"
PROXY = f"http://{PROXY_HOST}:3128"
PRINTER = "192.168.1.50"
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "WS_PROXY", "WSS_PROXY",
                   "MQTT_PROXY", "http_proxy", "https_proxy", "all_proxy", "mqtt_proxy")


@pytest.fixture
def environment(monkeypatch):
    """apply(itar): a public proxy in every proxy variable, ITAR mode on or off. The whole
    environment is put back afterwards (the neutralizer changes os.environ directly)."""
    saved = dict(os.environ)

    def refuse(self, address):
        raise AssertionError(f"a test tried to open a real connection to {address!r}")
    monkeypatch.setattr(socket.socket, "connect", refuse)

    def apply(itar_on):
        for name in [name for name in os.environ if name.lower().endswith("_proxy")]:
            del os.environ[name]
        os.environ.pop(itar.IGNORED_PROXIES_ENV, None)
        for name in PROXY_VARIABLES:
            os.environ[name] = PROXY
        if itar_on:
            os.environ["ODIN_ITAR_MODE"] = "1"
        else:
            os.environ.pop("ODIN_ITAR_MODE", None)
    yield apply
    os.environ.clear()
    os.environ.update(saved)


# ---- the neutralizer ----

def test_itar_mode_removes_every_proxy_variable_and_reports_the_names_only(environment, caplog):
    environment(True)
    os.environ["Socks5_Proxy"] = "socks5://user:hunter2@203.0.113.9:1080"
    os.environ["RSYNC_PROXY"] = "   "          # set but blank: removed, not reported
    with caplog.at_level("WARNING", logger="odin.itar"):
        ignored = itar.neutralize_environment_proxies()
    assert sorted(ignored) == sorted(PROXY_VARIABLES + ("Socks5_Proxy",))
    left = {name: value for name, value in os.environ.items() if name.lower().endswith("_proxy")}
    assert left == {"NO_PROXY": "*", "no_proxy": "*"}
    # Only NO_PROXY is left. urllib lists it under "no"; with it present urllib also skips
    # the operating system's proxy settings (macOS, Windows).
    assert urllib.request.getproxies_environment() == {"no": "*"}
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    for name in ignored:
        assert name in lines[0]
    assert PROXY_HOST not in lines[0] and "hunter2" not in lines[0] and "3128" not in lines[0]
    assert set(itar.ignored_environment_proxies()) == set(ignored)
    assert PROXY_HOST not in os.environ[itar.IGNORED_PROXIES_ENV]


def test_calling_it_again_changes_nothing_and_logs_nothing(environment, caplog):
    environment(True)
    first = itar.neutralize_environment_proxies()
    after_first = dict(os.environ)
    caplog.clear()
    with caplog.at_level("DEBUG", logger="odin.itar"):
        assert itar.neutralize_environment_proxies() == ()
    assert dict(os.environ) == after_first and caplog.records == []
    assert set(itar.ignored_environment_proxies()) == set(first)


def test_with_itar_mode_off_the_environment_is_not_touched(environment, caplog):
    environment(False)
    os.environ["NO_PROXY"] = "intranet.example"
    before = dict(os.environ)
    with caplog.at_level("DEBUG", logger="odin.itar"):
        assert itar.neutralize_environment_proxies() == ()
    assert dict(os.environ) == before and caplog.records == []
    assert itar.ignored_environment_proxies() == ()


def test_a_proxy_variable_is_any_name_ending_in_proxy_except_no_proxy():
    for name in ("HTTP_PROXY", "https_proxy", "All_Proxy", "FTP_PROXY", "WS_PROXY", "WSS_PROXY",
                 "MQTT_PROXY", "SOCKS_PROXY", "socks5_proxy", "GRPC_PROXY"):
        assert itar._is_proxy_variable(name), name
    for name in ("NO_PROXY", "no_proxy", "PROXY", "ODIN_TRUSTED_PROXIES", "HTTP_PROXY_HOST",
                 itar.IGNORED_PROXIES_ENV, "ODIN_ITAR_MODE", ""):
        assert not itar._is_proxy_variable(name), name


def test_the_report_lists_names_only_whatever_the_marker_holds(environment):
    environment(False)
    os.environ[itar.IGNORED_PROXIES_ENV] = "HTTPS_PROXY, http://user:pw@203.0.113.9:3128 ,NO_PROXY,HTTP_PROXY"
    assert itar.ignored_environment_proxies() == ("HTTPS_PROXY", "HTTP_PROXY")


def test_the_c_environment_and_child_processes_lose_the_proxy_too(environment):
    """ffmpeg inside OpenCV reads http_proxy with C getenv; ffmpeg, pg_dump and the Python
    workers ODIN starts inherit the environment."""
    environment(True)
    getenv = ctypes.CDLL(None).getenv
    getenv.restype = ctypes.c_char_p
    assert getenv(b"http_proxy") == PROXY.encode()
    itar.neutralize_environment_proxies()
    assert getenv(b"http_proxy") is None and getenv(b"HTTPS_PROXY") is None
    child = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         "import os; print(sorted((k, v) for k, v in os.environ.items() if k.lower().endswith('_proxy')))"],
        capture_output=True, text=True, check=True)
    assert child.stdout.strip() == "[('NO_PROXY', '*'), ('no_proxy', '*')]"


# ---- the two clients the review named: each guarded on its own, without the neutralizer ----

def _scheduler_request_destination(monkeypatch):
    """Where PrinterMonitor._trigger_reschedule would connect, and the headers it would send."""
    from modules.printers.monitors import mqtt_printer
    seen = []

    def create_connection(address, *args, **kwargs):
        seen.append(address)
        raise OSError("no network in tests")
    monkeypatch.setattr(socket, "create_connection", create_connection)
    # urlopen builds its opener once per process and keeps the proxies it saw then.
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(mqtt_printer, "Thread", lambda target, daemon: SimpleNamespace(start=target))
    monitor = mqtt_printer.PrinterMonitor.__new__(mqtt_printer.PrinterMonitor)
    monitor.name = "printer"
    monitor._trigger_reschedule()
    return seen


def test_the_scheduler_request_does_not_go_to_the_proxy_under_itar(environment, monkeypatch):
    environment(True)
    assert _scheduler_request_destination(monkeypatch) == [("localhost", 8000)]


def test_the_scheduler_request_is_unchanged_with_itar_off(environment, monkeypatch):
    environment(False)
    assert _scheduler_request_destination(monkeypatch) == [(PROXY_HOST, 3128)]


def _elegoo_destination(monkeypatch):
    """The addresses ElegooPrinter.connect() would open a socket to (real websocket-client)."""
    import websocket
    from modules.printers.adapters.elegoo import ElegooPrinter
    seen = []

    def open_socket(addrinfo_list, sockopt, timeout):
        seen.extend(info[4][:2] for info in addrinfo_list)
        raise OSError("no network in tests")
    monkeypatch.setattr(websocket._http, "_open_socket", open_socket)
    assert ElegooPrinter(PRINTER).connect() is False
    return seen


def test_the_elegoo_websocket_does_not_go_to_the_proxy_under_itar(environment, monkeypatch):
    environment(True)
    assert _elegoo_destination(monkeypatch) == [(PRINTER, 3030)]


def test_the_elegoo_websocket_is_unchanged_with_itar_off(environment, monkeypatch):
    environment(False)
    assert _elegoo_destination(monkeypatch) == [(PROXY_HOST, 3128)]


# ---- every library, built with no guard of its own: the neutralizer alone is enough ----
# Each check returns the proxy the library would use for a local-network address. The
# libraries are already imported when the neutralizer runs here, as uvicorn's are in the API.

def _httpx_proxy():
    with httpx.Client() as client:
        pool = client._transport_for_url(httpx.URL(f"http://{PRINTER}/"))._pool
    return getattr(pool, "_proxy_url", None) and bytes(pool._proxy_url.host).decode()


def _httpx_async_proxy():
    client = httpx.AsyncClient()
    pool = client._transport_for_url(httpx.URL(f"https://{PRINTER}/"))._pool
    return getattr(pool, "_proxy_url", None) and bytes(pool._proxy_url.host).decode()


def _requests_proxy():
    with requests.Session() as session:
        chosen = session.merge_environment_settings(f"http://{PRINTER}/", {}, None, None, None)["proxies"]
    return PROXY_HOST if any(PROXY_HOST in str(value) for value in chosen.values()) else None


def _urllib_proxy():
    opener = urllib.request.build_opener()
    proxies = [handler.proxies for handler in opener.handlers
               if isinstance(handler, urllib.request.ProxyHandler)]
    return PROXY_HOST if any(PROXY_HOST in str(found) for found in proxies) else None


def _websocket_client_proxy():
    import websocket
    return websocket._url.get_proxy_info(PRINTER, False)[0] or websocket._url.get_proxy_info(PRINTER, True)[0]


def _paho_proxy(monkeypatch):
    """paho reads `mqtt_proxy` only when PySocks is installed. ODIN does not ship PySocks;
    a stand-in shows the lookup is live when it is present."""
    import paho.mqtt.client as mqtt
    monkeypatch.setattr(mqtt, "socks", SimpleNamespace(HTTP=3, SOCKS4=1, SOCKS5=2, get_default_proxy=lambda: None))
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client._host = PRINTER
    return (client._get_proxy() or {}).get("proxy_addr")


def _aiohttp_proxy():
    helpers = pytest.importorskip("aiohttp.helpers")   # not an ODIN dependency; checked when installed
    import yarl
    try:
        return str(helpers.get_env_proxy_for_url(yarl.URL(f"http://{PRINTER}/"))[0].host)
    except LookupError:
        return None


def _websockets_proxy():
    uri = pytest.importorskip("websockets.uri")        # ODIN uses it only as uvicorn's server
    found = uri.get_proxy(uri.parse_uri(f"ws://{PRINTER}:3030/websocket"))
    return found and PROXY_HOST


LIBRARIES = {
    "httpx": lambda monkeypatch: _httpx_proxy(),
    "httpx async": lambda monkeypatch: _httpx_async_proxy(),
    "requests (also pywebpush)": lambda monkeypatch: _requests_proxy(),
    "urllib.request": lambda monkeypatch: _urllib_proxy(),
    "websocket-client": lambda monkeypatch: _websocket_client_proxy(),
    "paho-mqtt": _paho_proxy,
    "aiohttp": lambda monkeypatch: _aiohttp_proxy(),
    "websockets": lambda monkeypatch: _websockets_proxy(),
}


@pytest.mark.parametrize("library", sorted(LIBRARIES))
def test_no_library_selects_the_proxy_under_itar(environment, monkeypatch, library):
    environment(True)
    assert LIBRARIES[library](monkeypatch) == PROXY_HOST, "the check must see the proxy before it is removed"
    itar.neutralize_environment_proxies()
    assert LIBRARIES[library](monkeypatch) is None


@pytest.mark.parametrize("library", sorted(LIBRARIES))
def test_every_library_still_uses_the_proxy_with_itar_off(environment, monkeypatch, library):
    environment(False)
    itar.neutralize_environment_proxies()
    assert LIBRARIES[library](monkeypatch) == PROXY_HOST


def test_no_proxy_star_alone_stops_a_proxy_that_is_set_afterwards(environment, monkeypatch):
    """The second layer: a proxy variable that reappears after the neutralizer ran."""
    environment(True)
    itar.neutralize_environment_proxies()
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = os.environ["HTTPS_PROXY"] = PROXY
    os.environ["https_proxy"] = os.environ["mqtt_proxy"] = PROXY
    assert _requests_proxy() is None
    assert _websocket_client_proxy() is None
    assert _paho_proxy(monkeypatch) is None
    assert _aiohttp_proxy() is None
    assert _websockets_proxy() is None
    assert urllib.request.proxy_bypass_environment(PRINTER) and urllib.request.proxy_bypass_environment("localhost")
    with httpx.Client() as client:   # httpx mounts "no proxy" for all hosts ahead of the proxy
        assert getattr(client._transport_for_url(httpx.URL(f"http://{PRINTER}/"))._pool, "_proxy_url", None) is None


# ---- every way an ODIN process starts reaches the neutralizer before a network library ----

# Importing any of these (or a submodule) can pick a proxy up from the environment.
NETWORK_LIBRARIES = ("requests", "httpx", "urllib.request", "urllib3", "aiohttp", "http.client", "smtplib",
                     "ftplib", "paho", "websocket", "websockets", "pywebpush", "authlib", "cv2")
# Third-party packages an entry point imports before ODIN's own code. None of them imports a
# network library (checked in a fresh interpreter below).
NO_NETWORK_PACKAGES = ("sqlalchemy", "passlib", "cryptography")
# Importing either package runs the neutralizer first (its __init__.py).
HOOK_PACKAGES = ("core", "modules")
# Start without any ODIN package and without any network library. File -> what it does.
NO_ODIN_IMPORT = {
    "backend/scripts/demo_seed_edu.py": "SQLite and passlib only",
    "backend/scripts/demo_seed_reviewer.py": "runs demo_seed_edu",
    "ops/edu_sandbox/tcp_proxy.py": "relays between two sockets it is given; no client library",
    "docker/runtime_identity.py": "writes one file",
    "docker/branding_defaults.py": "writes one file",
    "docker/sqlite_image_check.py": "checks the SQLite build",
}
# Programs supervisord starts that are not Python. docker/entrypoint.sh removes the proxy
# settings before supervisord starts, so these inherit none.
OTHER_SUPERVISOR_PROGRAMS = {"/usr/local/bin/go2rtc"}
# Files that start another Python process. The child inherits the parent's environment, which
# the parent has already cleaned. File -> what it starts.
PYTHON_SPAWNERS = {
    "core/media_probe_capability.py": ["core/timelapse_probe_worker.py"],
    "core/media_derivative_capability.py": ["core/timelapse_derivative_worker.py"],
    "core/media_process_capability.py": ["core/timelapse_media_worker.py"],
    "core/sender_process_group.py": ["core/sender_process_group.py"],
    "modules/models_library/mesh_isolation.py": ["modules/models_library/mesh_worker.py"],
    "modules/models_library/mesh_worker.py": [],     # `-c "import time; time.sleep(60)"`
    "modules/system/backup_schedule.py": ["scripts/bootstrap_database.py"],
    "modules/system/troubleshooting.py": [],         # `-c` name lookup: socket only
}


def _dotted(name, prefixes):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def imports_in_order(source, filename="<source>"):
    """Every absolute import in a Python source, in source order (functions included)."""
    found = []

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node):
            found.extend(alias.name for alias in node.names)

        def visit_ImportFrom(self, node):
            if not node.level and node.module:
                found.append(node.module)
                found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    Visitor().visit(ast.parse(source, filename=filename))
    return found


def _local_file(name, beside):
    """A plain module of this project (not in the core/modules packages), as a path."""
    parts = name.removeprefix("backend.").split(".")
    for base in (beside, BACKEND, BACKEND / "scripts"):
        for count in range(len(parts), 0, -1):
            candidate = base.joinpath(*parts[:count]).with_suffix(".py")
            if candidate.is_file():
                return candidate
    return None


def reaches_the_neutralizer_first(path, _seen=None):
    """'hook' when the first import that is not plain standard library is ODIN's `core` or
    `modules` package; 'none' when the file never imports ODIN and imports no network
    library. Raises AssertionError naming the import that comes too early."""
    seen = _seen if _seen is not None else set()
    if path in seen:
        return "none"
    seen.add(path)
    for name in imports_in_order(path.read_text(encoding="utf-8"), str(path)):
        if _dotted(name.removeprefix("backend."), HOOK_PACKAGES):
            return "hook"
        if _dotted(name, NETWORK_LIBRARIES):
            raise AssertionError(f"{path.relative_to(ROOT)} imports {name} before ODIN's core or modules package")
        if name.split(".")[0] in sys.stdlib_module_names or _dotted(name, NO_NETWORK_PACKAGES):
            continue
        local = _local_file(name, path.parent)
        if local is None:
            if name.split(".")[0] in ("scripts", "backend"):
                continue   # the other spellings of a sibling script: resolved under their own name
            raise AssertionError(f"{path.relative_to(ROOT)} imports {name} before ODIN's core or modules "
                                 "package; review it and add it to NO_NETWORK_PACKAGES if it is safe")
        if reaches_the_neutralizer_first(local, seen) == "hook":
            return "hook"
    return "none"


def _supervisor_commands():
    commands = []
    for conf in sorted((ROOT / "docker").glob("supervisord*.conf")):
        commands += re.findall(r"^command=(.+)$", conf.read_text(encoding="utf-8"), flags=re.M)
    return commands


def _shell_python_commands():
    """(script, how, target) for every python3 use in the container's start-up scripts."""
    found = []
    for script in ("docker/entrypoint.sh", "docker/seed_edu_if_enabled.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        matches = re.findall(r"python3 (?:-I -S )?(-m|-c|/)\s?([\w./-]*)", text)
        assert len(matches) == len(re.findall(r"\bpython3?\b(?!\.)", text)), \
            f"{script} runs Python in a form this test does not recognise"
        found += [(script, how, target) for how, target in matches]
    return found


def _main_guard_files():
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith("core/schema/preserved") or "/tests/" in relative or relative.startswith("tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test) and "__main__" in ast.unparse(node.test):
                found.append(relative)
                break
    return found


def entry_points():
    """Every way a Python ODIN process starts, as paths relative to the repository, from:
    the supervisord configurations, the container start-up scripts, the files the Dockerfile
    copies from ops/ and docker/, every backend file with an `if __name__ == "__main__"`
    block, and the Python workers ODIN starts itself."""
    files = set()
    for command in _supervisor_commands():
        if command.startswith("python3 -m uvicorn main:app "):
            files.add("backend/main.py")
        elif command.startswith("python3 -m "):
            files.add("backend/" + command.split()[2].replace(".", "/") + ".py")
        else:
            assert command.split()[0] in OTHER_SUPERVISOR_PROGRAMS, f"unreviewed supervisord program: {command}"
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = dict((target, source) for source, target in re.findall(r"^COPY ((?:docker|ops)/\S+\.py) (\S+)$", dockerfile, flags=re.M))
    for script, how, target in _shell_python_commands():
        if how == "-m":
            files.add("backend/" + target.replace(".", "/") + ".py")
        elif how == "/":
            files.add(copied["/" + target])
    files.update(source for source in copied.values() if source.startswith("ops/"))
    files.update("backend/" + name for name in _main_guard_files())
    files.update("backend/" + child for children in PYTHON_SPAWNERS.values() for child in children)
    return sorted(files)


def test_the_entry_point_list_has_the_processes_the_container_runs():
    found = entry_points()
    for expected in (
        "backend/main.py", "backend/modules/printers/monitors/mqtt_monitor.py",
        "backend/modules/printers/monitors/moonraker_monitor.py",
        "backend/modules/printers/monitors/prusalink_monitor.py",
        "backend/modules/printers/monitors/elegoo_monitor.py",
        "backend/modules/printers/monitors/anycubic_lan_monitor.py",
        "backend/modules/vision/monitor.py", "backend/modules/archives/timelapse_capture.py",
        "backend/modules/reporting/report_runner.py", "backend/modules/system/db_guardian.py",
        "backend/modules/system/restore_coordinator.py", "backend/scripts/bootstrap_database.py",
        "backend/scripts/wait_for_database_schema.py", "backend/scripts/prepare_go2rtc_config.py",
        "backend/scripts/demo_seed_edu.py", "backend/scripts/seed_edu_sandbox.py",
        "backend/modules/models_library/mesh_worker.py", "backend/core/timelapse_media_worker.py",
        "ops/demo/demo_publisher.py", "ops/edu_sandbox/tcp_proxy.py", "docker/runtime_identity.py",
    ):
        assert expected in found, expected
    for path in found:
        assert (ROOT / path).is_file(), path


@pytest.mark.parametrize("entry", entry_points())
def test_every_entry_point_reaches_the_neutralizer_before_a_network_library(entry):
    path = ROOT / entry
    relative = path.relative_to(BACKEND).as_posix() if entry.startswith("backend/") else entry
    if relative.split("/")[0] in HOOK_PACKAGES and relative not in sum(PYTHON_SPAWNERS.values(), []):
        # Started as `python3 -m core.x` / `-m modules.x`: Python imports the package, and so
        # runs its __init__.py, before the first line of the file.
        return
    outcome = reaches_the_neutralizer_first(path)
    if entry in NO_ODIN_IMPORT:
        assert outcome == "none", f"{entry} now imports ODIN code; remove it from NO_ODIN_IMPORT"
    elif relative.split("/")[0] in HOOK_PACKAGES:
        # A worker ODIN starts by file path: it inherits a cleaned environment, and it must
        # not import a network library before (or without) the ODIN package.
        assert outcome in ("hook", "none")
    else:
        assert outcome == "hook", f"{entry} does not import ODIN's core or modules package; classify it in NO_ODIN_IMPORT"


def test_the_container_scripts_start_package_modules_with_dash_m_only():
    """`python3 path/to/file.py` would skip the package __init__.py."""
    for script, how, target in _shell_python_commands():
        assert how in ("-m", "-c") or target.startswith("usr/local/libexec/odin-"), (script, how, target)
    for command in _supervisor_commands():
        assert command.startswith("python3 -m ") or command.split()[0] in OTHER_SUPERVISOR_PROGRAMS, command
    for compose in ("docker-compose.yml", "docker-compose.enterprise.yml", "install/docker-compose.yml"):
        text = (ROOT / compose).read_text(encoding="utf-8")
        for command in re.findall(r"^\s+(?:command|entrypoint):\s*(.+)$", text, flags=re.M):
            assert "/usr/bin/supervisord" in command or "pg_isready" in command, (compose, command)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/usr/local/bin/odin-entrypoint"]' in dockerfile
    assert 'CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/odin.conf"]' in dockerfile


@pytest.mark.parametrize("package", HOOK_PACKAGES)
def test_the_package_runs_the_neutralizer_before_anything_else(package):
    tree = ast.parse((BACKEND / package / "__init__.py").read_text(encoding="utf-8"))
    statements = [node for node in tree.body
                  if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    assert statements, f"{package}/__init__.py does not run the neutralizer"
    # The flag check comes first: a value that is neither on nor off stops the process.
    first, check, call = statements[0], statements[1], statements[2]
    names = "neutralize_environment_proxies, refuse_malformed_itar_flag"
    if isinstance(first, ast.Try):   # modules/: also importable as backend.modules
        assert ast.unparse(first.handlers[0].body[0]) == f"from ..core.itar import {names}"
        first = first.body[0]
    # core/ imports its own module relatively, so it also works as backend.core.
    assert ast.unparse(first) == (f"from .itar import {names}" if package == "core" else f"from core.itar import {names}")
    assert ast.unparse(check) == "refuse_malformed_itar_flag()"
    assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call) and not call.value.args
    assert ast.unparse(call.value.func) == "neutralize_environment_proxies"


@pytest.mark.parametrize("statement, directory", [
    ("import core", BACKEND),                 # the API (main.py), the scripts
    ("import modules", BACKEND),              # python3 -m modules.x: every monitor and worker
    ("import backend.modules", ROOT),         # ops/demo/demo_publisher.py
])
@pytest.mark.parametrize("mode", ["1", ""])
def test_importing_the_package_cleans_a_fresh_process(statement, directory, mode):
    """A real interpreter: the proxy is gone as soon as the package is imported, and the
    report reaches stderr although logging is not configured yet."""
    program = (f"{statement}\nimport os\n"
               "print(sorted((k, v) for k, v in os.environ.items() if k.lower().endswith('_proxy')))")
    ran = subprocess.run(
        [sys.executable, "-c", program], cwd=directory, capture_output=True, text=True, check=True,
        env={"PATH": os.environ.get("PATH", ""), "ODIN_ITAR_MODE": mode,
             "HTTP_PROXY": "http://user:hunter2@203.0.113.9:3128", "https_proxy": PROXY})
    if mode:
        assert ran.stdout.strip() == "[('NO_PROXY', '*'), ('no_proxy', '*')]"
        assert "ignoring the proxy settings HTTP_PROXY, https_proxy." in ran.stderr
        assert "hunter2" not in ran.stderr and PROXY_HOST not in ran.stderr
    else:
        assert ran.stdout.strip() == ("[('HTTP_PROXY', 'http://user:hunter2@203.0.113.9:3128'), "
                                      f"('https_proxy', '{PROXY}')]")
        assert ran.stderr == ""


def test_core_itar_itself_imports_no_network_library():
    names = imports_in_order((BACKEND / "core/itar.py").read_text(encoding="utf-8"))
    top_level = [name for name in names if not _dotted(name, HOOK_PACKAGES + ("sqlalchemy",))]
    assert not [name for name in top_level if _dotted(name, NETWORK_LIBRARIES)]


def test_the_packages_imported_before_odin_code_bring_no_network_library():
    listing = subprocess.run(
        [sys.executable, "-c",
         "import sys\nimport sqlalchemy, sqlalchemy.engine, sqlalchemy.orm, passlib.context, cryptography.fernet\n"
         "print('\\n'.join(sorted(sys.modules)))"],
        capture_output=True, text=True, check=True, env={"PATH": os.environ.get("PATH", "")})
    loaded = [name for name in listing.stdout.split() if _dotted(name, NETWORK_LIBRARIES)]
    assert loaded == []


def test_every_file_that_starts_a_python_process_is_reviewed():
    found = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith("core/schema/preserved") or "/tests/" in relative or relative.startswith("tests/"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=relative)):
            if isinstance(node, ast.Attribute) and ast.unparse(node) == "sys.executable":
                found.add(relative)
    assert found == set(PYTHON_SPAWNERS), "a file starts a Python process: say what it starts in PYTHON_SPAWNERS"


def test_no_child_process_is_given_a_proxy_variable():
    """A child inherits the cleaned environment; nothing may add a proxy back for it."""
    offenders = []
    for path in list(BACKEND.rglob("*.py")) + [ROOT / "docker/entrypoint.sh", ROOT / "docker/seed_edu_if_enabled.sh",
                                                *sorted((ROOT / "docker").glob("supervisord*.conf"))]:
        relative = path.relative_to(ROOT).as_posix()
        if "core/schema/preserved" in relative or "/tests/" in relative or relative == "backend/core/itar.py":
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix != ".py":
            lines = [line for line in text.splitlines() if not line.lstrip().startswith(("#", ";"))]
            start = text.index("# ITAR-PROXIES-BEGIN") if "# ITAR-PROXIES-BEGIN" in text else None
            if start is not None:
                block = text[start:text.index("# ITAR-PROXIES-END")]
                lines = [line for line in lines if line not in block.splitlines()]
            if re.search(r"(?i)\b\w*_proxy\s*=", "\n".join(lines)):
                offenders.append(relative)
            continue
        for node in ast.walk(ast.parse(text, filename=relative)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and itar._is_proxy_variable(node.value.strip()):
                offenders.append(relative)
    assert offenders == []


# ---- the container entrypoint: the programs supervisord starts (go2rtc included) ----

def _entrypoint_block():
    text = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    return text[text.index("# ITAR-PROXIES-BEGIN"):text.index("# ITAR-PROXIES-END")]


def _run_entrypoint_block(environment):
    script = _entrypoint_block() + "\nenv\n"
    return subprocess.run(["/bin/bash", "-c", script], env={"PATH": os.environ.get("PATH", ""), **environment},
                          capture_output=True, text=True, check=True).stdout.splitlines()


def test_the_entrypoint_removes_the_proxy_settings_before_any_python_or_supervisord():
    text = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    assert text.index("# ITAR-PROXIES-END") < text.index("python3")
    assert text.index("# ITAR-PROXIES-END") < text.index('exec "$@"')


# The shell must read the value exactly as Python does, white space included: a value with a
# carriage return or a leading newline (a Windows-edited env file) once turned ITAR mode on in
# Python while the shell block did nothing, leaving go2rtc with the proxy. Values that are
# neither on nor off are refused by both: tests/test_contracts/test_itar_flag_spellings.py.
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 ", "1\r", "\n1", "\t true \r\n", "\v\fON\f"])
def test_the_entrypoint_block_matches_the_python_rule_with_itar_on(value, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", value)
    assert itar.is_itar_mode()
    lines = _run_entrypoint_block({
        "ODIN_ITAR_MODE": value, "HTTP_PROXY": "http://user:hunter2@203.0.113.9:3128", "https_proxy": PROXY,
        "ALL_PROXY": PROXY, "Mqtt_Proxy": PROXY, "FTP_PROXY": "", "NO_PROXY": "intranet.example",
        "ODIN_TRUSTED_PROXIES": "10.0.0.1",
    })
    report = [line for line in lines if "ignoring the proxy settings" in line]
    assert len(report) == 1 and "hunter2" not in report[0] and PROXY_HOST not in report[0]
    variables = dict(line.split("=", 1) for line in lines if "=" in line and line not in report)
    assert {name: value for name, value in variables.items() if name.lower().endswith("_proxy")} == \
        {"NO_PROXY": "*", "no_proxy": "*"}
    assert variables["ODIN_TRUSTED_PROXIES"] == "10.0.0.1"
    assert variables[itar.IGNORED_PROXIES_ENV].split(",") == ["ALL_PROXY", "HTTP_PROXY", "Mqtt_Proxy", "https_proxy"]
    for name in variables[itar.IGNORED_PROXIES_ENV].split(","):
        assert name in report[0]


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "OFF", " No ", " ", "\t", " 0 "])
def test_the_entrypoint_block_changes_nothing_with_itar_off(value, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", value)
    assert not itar.is_itar_mode()
    given = {"ODIN_ITAR_MODE": value, "HTTP_PROXY": PROXY, "https_proxy": PROXY, "NO_PROXY": "intranet.example"}
    lines = _run_entrypoint_block(given)
    variables = dict(line.split("=", 1) for line in lines if "=" in line)
    assert {name: variables.get(name) for name in given} == given
    assert itar.IGNORED_PROXIES_ENV not in variables and "no_proxy" not in variables
    assert not [line for line in lines if "ignoring" in line]


def test_the_container_health_check_never_uses_a_proxy():
    """Docker runs the health check with the container's own environment, not the entrypoint's."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "CMD curl --noproxy '*' -f http://localhost:8000/health/ready || exit 1" in dockerfile
    for compose in ("docker-compose.yml", "docker-compose.enterprise.yml", "install/docker-compose.yml"):
        for line in (ROOT / compose).read_text(encoding="utf-8").splitlines():
            if "curl" in line and "localhost" in line and not line.lstrip().startswith("#"):
                assert '"--noproxy", "*"' in line, (compose, line)


# ---- the boot report ----

def test_the_boot_audit_reports_the_ignored_proxy_settings_and_still_starts(environment, caplog):
    environment(True)
    itar.neutralize_environment_proxies()
    with caplog.at_level("INFO", logger="odin.itar"):
        itar.enforce_boot_config(["http://192.168.1.20:7912"])      # does not raise
    passed = [record.getMessage() for record in caplog.records if "boot audit passed" in record.getMessage()]
    assert len(passed) == 1 and "HTTP_PROXY" in passed[0] and "https_proxy" in passed[0]
    assert "were set and are ignored" in passed[0] and PROXY_HOST not in passed[0]


def test_the_boot_audit_says_nothing_about_proxies_when_none_was_set(environment, caplog):
    environment(True)
    for name in PROXY_VARIABLES:
        del os.environ[name]
    itar.neutralize_environment_proxies()
    with caplog.at_level("INFO", logger="odin.itar"):
        itar.enforce_boot_config(["http://192.168.1.20:7912"])
    assert not [record for record in caplog.records if "roxy" in record.getMessage()]
