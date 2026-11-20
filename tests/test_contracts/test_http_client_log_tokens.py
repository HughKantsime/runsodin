"""Notification credentials stay out of new requests and previously written logs."""
import ast
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from time import perf_counter

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SCRUB = "scripts.scrub_http_client_log_lines"
MARKER = ".http_client_log_scrub_done"

DESTINATIONS = {
    "telegram": "123:synthetic-telegram|12345",
    "discord": "https://discord.com/api/webhooks/123/synthetic-discord",
    "slack": "https://hooks.slack.com/services/T/B/synthetic-slack",
    "generic": "https://example.test/custom/synthetic-generic",
}


@pytest.mark.parametrize("kind,install_first", [
    ("telegram", False), ("discord", False), ("slack", False), ("generic", False),
    ("generic", True),
])
def test_delivery_omits_http_request_lines(tmp_path, kind, install_first):
    log = tmp_path / "monitor.log"
    subprocess.run([sys.executable, "-c", """
import logging
import socket
import sys
from unittest.mock import patch

import httpx
from core.log_redaction import install_log_redaction
from modules.notifications.webhook_payloads import post_typed_webhook

kind, url, log, order = sys.argv[1:]
if order == "first":
    install_log_redaction()
logging.basicConfig(filename=log, level=logging.INFO)
if order == "last":
    install_log_redaction()

answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
def respond(self, request):
    return httpx.Response(200, json={}, request=request)

with patch("socket.getaddrinfo", return_value=answer), patch.object(httpx.HTTPTransport, "handle_request", respond):
    assert post_typed_webhook(url, kind, event="test", title="Test", message="Test") is True
logging.info("Delivery finished")
for name in ("httpx", "httpcore"):
    assert logging.getLogger(name).level == logging.WARNING
    logging.getLogger(name).setLevel(logging.DEBUG)
install_log_redaction()
for name in ("httpx", "httpcore"):
    assert logging.getLogger(name).level == logging.WARNING
""", kind, DESTINATIONS[kind], str(log), "first" if install_first else "last"],
                   cwd=BACKEND, env={**os.environ, "ODIN_ITAR_MODE": "0"}, check=True)
    output = log.read_text()
    assert "Delivery finished" in output
    assert "synthetic" not in output
    assert "HTTP Request:" not in output


@pytest.mark.parametrize("method,url,expected_url", [
    ("POST", "https://api.telegram.org/bot123:synthetic-telegram/sendMessage", "https://api.telegram.org/[REDACTED]"),
    ("POST", DESTINATIONS["discord"], "https://discord.com/[REDACTED]"),
    ("POST", "https://example.test/run?api-version=1&sv=1.0&sig=synthetic-value", "https://example.test/[REDACTED]"),
    ("GET", "https://example.test?sig=synthetic-value", "https://example.test/[REDACTED]"),
    ("PUT", "http://example.test:8443#synthetic-value", "http://example.test:8443/[REDACTED]"),
    ("PATCH", "https://synthetic-user@example.test/path", "https://example.test/[REDACTED]"),
    ("HEAD", "https://example.test:8443", "https://example.test:8443"),
])
def test_sanitize_stored_request(method, url, expected_url):
    from modules.system.sanitize import sanitize_log_lines
    [line] = sanitize_log_lines([f'HTTP Request: {method} {url} "HTTP/1.1 200 OK"'])
    assert line == f'HTTP Request: {method} {expected_url} "HTTP/1.1 200 OK"'


def test_redact_long_http_request_line():
    from core.log_redaction import redact_text
    path = "/hook" * ((1024 * 1024) // 5 + 1)
    line = f'HTTP Request: GET https://example.test{path}?sig=synthetic-value "HTTP/1.1 200 OK"'
    start = perf_counter()
    clean = redact_text(line)
    assert perf_counter() - start < 2
    assert clean == 'HTTP Request: GET https://example.test/[REDACTED] "HTTP/1.1 200 OK"'


def test_supervised_python_modules_install_redaction():
    modules = set()
    for conf in (ROOT / "docker").glob("supervisord*.conf"):
        for command in re.findall(r"^command=python3 -m (.+)$", conf.read_text(), re.M):
            module, *args = command.split()
            if module == "uvicorn":
                assert "main:app" in args
                module = "core.app"
            modules.add(module)
    assert "core.app" in modules
    for module in modules:
        source = BACKEND.joinpath(*module.split(".")).with_suffix(".py")
        assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "install_log_redaction"
                   for node in ast.walk(ast.parse(source.read_text()))), module


def _scrub(data):
    return subprocess.run([sys.executable, "-m", SCRUB, str(data)], cwd=BACKEND,
                          capture_output=True, text=True, check=True)


def test_scrub_current_and_rotated_logs_once(tmp_path):
    request = b'HTTP Request: POST https://example.test/custom/synthetic-value "HTTP/1.1 200 OK"'
    clean = request.replace(b"custom/synthetic-value", b"[REDACTED]")
    query = b'HTTP Request: GET https://example.test:8443?auth=synthetic-value "HTTP/1.1 200 OK"'
    clean_query = query.replace(b"?auth=synthetic-value", b"/[REDACTED]")
    ordinary = b"\xffordinary\r\nhttps://example.test/hooks/synthetic-ordinary\n"
    long_prefix = b" " * (1024 * 1024) + b"\xfe"
    for name in ("monitor.log", "monitor.log.12"):
        path = tmp_path / name
        path.write_bytes(ordinary + request + b"\r\n" + long_prefix + query)
        path.chmod(0o640)
    unchanged = tmp_path / "backend.log"
    unchanged.write_bytes(ordinary + b"HTTP Request: GET https://example.test\n")
    before = unchanged.stat()
    (tmp_path / "ignored.log.gz").write_bytes(request)
    (tmp_path / "x.log").symlink_to(tmp_path / "ignored.log.gz")
    (tmp_path / "directory.log").mkdir()
    (tmp_path / "directory.log" / "nested.log").write_bytes(request)
    os.mkfifo(tmp_path / "pipe.log")

    result = _scrub(tmp_path)
    assert result.stdout.splitlines() == ["Log cleanup: 3 files examined, 2 files rewritten, 4 lines redacted."]
    assert result.stderr == ""
    assert (tmp_path / MARKER).read_bytes() == b""
    for name in ("monitor.log", "monitor.log.12"):
        path = tmp_path / name
        assert path.read_bytes() == ordinary + clean + b"\r\n" + long_prefix + clean_query
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert (tmp_path / "x.log").is_symlink()
    assert (tmp_path / "ignored.log.gz").read_bytes() == request
    assert (tmp_path / "directory.log" / "nested.log").read_bytes() == request
    assert (unchanged.stat().st_ino, unchanged.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    late = tmp_path / "monitor.log.1"
    late.write_bytes(request)
    assert "0 files examined" in _scrub(tmp_path).stdout
    assert late.read_bytes() == request


def test_scrub_unreadable_log_warns_and_retries(tmp_path):
    request = b"HTTP Request: POST https://example.test/custom/synthetic-value\n"
    blocked = tmp_path / "blocked.log"
    blocked.write_bytes(request)
    blocked.chmod(0)
    other = tmp_path / "other.log.1"
    other.write_bytes(request)
    try:
        if os.access(blocked, os.R_OK):
            # Root can read mode 000. Reproduce odin's open failure in that case.
            result = subprocess.run([sys.executable, "-c", """
import runpy, sys
def deny_open(event, args):
    if event == "open" and args[0] == sys.argv[1] + "/blocked.log":
        raise PermissionError
sys.addaudithook(deny_open)
runpy.run_module("scripts.scrub_http_client_log_lines", run_name="__main__")
""", str(tmp_path)], cwd=BACKEND, capture_output=True, text=True, check=True)
        else:
            result = _scrub(tmp_path)
    finally:
        blocked.chmod(0o600)
    assert result.stderr.splitlines() == ["WARNING: log scrub failed for blocked.log: PermissionError"]
    assert "synthetic" not in result.stdout
    assert blocked.read_bytes() == request
    assert b"synthetic" not in other.read_bytes()
    assert not (tmp_path / MARKER).exists()
    assert _scrub(tmp_path).stderr == ""
    assert b"synthetic" not in blocked.read_bytes()
    assert (tmp_path / MARKER).is_file()


def test_scrub_write_failure_keeps_original(tmp_path, monkeypatch, capsys):
    from scripts import scrub_http_client_log_lines
    path = tmp_path / "monitor.log"
    original = b"HTTP Request: POST https://example.test/custom/synthetic-value\n"
    path.write_bytes(original)
    inode = path.stat().st_ino

    def fail_sync(fd):
        raise OSError("synthetic failure detail")

    monkeypatch.setattr(os, "fsync", fail_sync)
    scrub_http_client_log_lines.main(tmp_path)
    output = capsys.readouterr()
    assert output.err.splitlines() == ["WARNING: log scrub failed for monitor.log: OSError"]
    assert "0 files rewritten, 0 lines redacted" in output.out
    assert list(tmp_path.iterdir()) == [path]
    assert path.stat().st_ino == inode
    assert path.read_bytes() == original


def test_entrypoint_scrubs_as_odin_before_supervisor():
    script = (ROOT / "docker" / "entrypoint.sh").read_text()
    scrub = script.index("/usr/sbin/runuser -u odin -- python3 -m " + SCRUB)
    owner = script.index('if [ "${ODIN_DB_BOOTSTRAP_OWNER:-1}" = "1" ]; then')
    assert script.index("-exec chown odin:odin") < owner < scrub < script.index("set -- /usr/bin/supervisord")
