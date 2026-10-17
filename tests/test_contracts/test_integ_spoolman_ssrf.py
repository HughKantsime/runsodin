"""Review fix O1: one Spoolman base-URL validator, on save and on every call.

The URL check accepted http://127.0.0.1:1984/api/streams?x= (a query string makes every
"{base}/api/v1/spool" call land on go2rtc's API), and the GET routes fetched with no
destination policy at all and reflected the raw upstream body. Now every Spoolman call
(GET and PUT) and the save go through core.spoolman_client: no userinfo, query or
fragment; resolved and pinned addresses that pass the device policy (no loopback,
link-local, metadata) and are not ODIN's own service endpoints; no redirects. Routes
return only the fields ODIN uses."""
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SUPER = {"id": 1, "role": "admin", "group_id": None, "is_active": True}
BAD = ["http://127.0.0.1:1984/api/streams?x=", "http://spoolman.test:7912/#frag", "http://user:pw@spoolman.test:7912",
       "http://169.254.169.254", "http://[::1]:7912", "http://localhost:7912"]


@pytest.fixture
def app(review_db, monkeypatch):  # noqa: F811
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.inventory.routes import drying_logs, spoolman
    from modules.system import routes_health
    application = FastAPI()
    for router in (spoolman.router, drying_logs.router, routes_health.router):
        application.include_router(router, prefix="/api")
    application.dependency_overrides[get_db] = lambda: review_db
    application.dependency_overrides[get_current_user] = lambda: SUPER
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def connects(monkeypatch):
    attempts = []
    real = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and address[0] not in ("testserver",):
            attempts.append(address)
            raise ConnectionRefusedError("test: no outbound connection")
        return real(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
    return attempts


def _configure(monkeypatch, url):
    from core.config import settings
    monkeypatch.setattr(settings, "spoolman_url", url)


@pytest.mark.parametrize("url", BAD)
def test_saving_a_refused_spoolman_url_is_400(app, url, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "spoolman_url", "")
    monkeypatch.delenv("SPOOLMAN_URL", raising=False)
    response = app.put("/api/config", json={"spoolman_url": url})
    assert response.status_code in (400, 422), (url, response.status_code, response.text)


@pytest.mark.parametrize("url", BAD)
@pytest.mark.parametrize("path", ["/api/spoolman/spools", "/api/spoolman/filaments", "/api/spoolman/sync",
                                  "/api/filaments/combined", "/api/spoolman/test"])
def test_calls_with_a_refused_stored_url_never_connect(app, connects, monkeypatch, url, path):
    _configure(monkeypatch, url)
    method = app.post if path.endswith("/sync") else app.get
    response = method(path)
    assert connects == [], (url, path, connects)
    assert response.status_code != 200 or "spool_" not in response.text, response.text


@pytest.mark.parametrize("url", BAD)
def test_consumption_put_with_a_refused_url_never_connects(connects, monkeypatch, url):
    from modules.inventory.services import deliver_spoolman_usage
    _configure(monkeypatch, url)
    state, reason = deliver_spoolman_usage(42, 1.5)
    assert state == "failed" and connects == [], (state, reason, connects)


def test_own_service_endpoint_is_refused(monkeypatch):
    from core import webhook_utils
    from core.spoolman_client import SpoolmanURLRefused, validate_spoolman_base
    monkeypatch.setattr(webhook_utils, "own_interface_addresses", lambda: frozenset({__import__("ipaddress").ip_address("10.9.8.7")}))
    with pytest.raises(SpoolmanURLRefused):
        validate_spoolman_base("http://10.9.8.7:8000")
    assert validate_spoolman_base("http://10.9.8.7:7912/") == "http://10.9.8.7:7912"  # Spoolman beside ODIN


class _Peer:
    def __init__(self, payloads):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps(payloads.get(self.path.split("?")[0], {})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_routes_return_only_the_fields_odin_uses(app, monkeypatch):
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)  # the Spoolman double listens on 127.0.0.1
    filament = {"id": 3, "name": "PLA Black", "material": "PLA", "color_hex": "000000", "vendor": {"name": "V", "extra": "x"},
                "internal_note": "UPSTREAM-ONLY-SECRET", "diameter": 1.75}
    spool = {"id": 7, "remaining_weight": 500, "filament": filament, "comment": "UPSTREAM-ONLY-SECRET"}
    peer = _Peer({"/api/v1/filament": [filament], "/api/v1/spool": [spool], "/api/v1/health": {"status": "ok"}})
    try:
        _configure(monkeypatch, peer.url)
        for path in ("/api/spoolman/filaments", "/api/spoolman/spools", "/api/filaments/combined"):
            response = app.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "UPSTREAM-ONLY-SECRET" not in response.text, (path, response.text)
        assert app.get("/api/spoolman/filaments").json() == [
            {"id": 3, "name": "PLA Black", "vendor": {"name": "V"}, "material": "PLA", "color_hex": "000000",
             "diameter": 1.75, "density": None, "weight": None}]
    finally:
        peer.close()


def test_no_backend_code_calls_spoolman_outside_the_client():
    offenders = []
    for path in (ROOT / "backend").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/schema/preserved" in rel or rel.endswith("core/spoolman_client.py"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\{(spoolman_url|base)\}/api/v1/(spool|filament|health)", line):
                offenders.append(f"{rel}:{number}")
    assert offenders == []
