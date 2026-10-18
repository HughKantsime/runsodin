"""Review fix O1-b: Spoolman on the same host is a supported setup.

O1 refused every loopback Spoolman URL, which broke Spoolman beside a bare-metal or
host-network ODIN install (commonly http://localhost:7912). Loopback and private
addresses are allowed again; ODIN's own service endpoints (8554, 8555, 1984, 8000 and the
configured ports, on loopback or on ODIN's own addresses), metadata and link-local
addresses, and userinfo, query strings and fragments stay refused."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def _no_itar(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)


def test_localhost_spoolman_is_accepted():
    from core.spoolman_client import validate_spoolman_base
    assert validate_spoolman_base("http://localhost:7912") == "http://localhost:7912"
    assert validate_spoolman_base("http://127.0.0.1:7912/") == "http://127.0.0.1:7912"
    assert validate_spoolman_base("http://[::1]:7912") == "http://[::1]:7912"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1984", "http://127.0.0.1:8000", "http://localhost:8554", "http://[::1]:8555",
    "http://169.254.169.254", "http://169.254.169.254:7912", "http://169.254.1.1:7912",
    "http://[fe80::1]:7912", "http://0.0.0.0:7912", "http://[::ffff:127.0.0.1]:1984",
    "http://localhost:7912?x=1", "http://localhost:7912/#f", "http://u:p@localhost:7912",
])
def test_own_services_metadata_and_link_local_stay_refused(url):
    from core.spoolman_client import SpoolmanURLRefused, validate_spoolman_base
    with pytest.raises(SpoolmanURLRefused):
        validate_spoolman_base(url)


def test_configured_odin_port_on_loopback_is_refused(monkeypatch):
    from core.spoolman_client import SpoolmanURLRefused, validate_spoolman_base
    monkeypatch.setenv("PORT", "18080")
    with pytest.raises(SpoolmanURLRefused):
        validate_spoolman_base("http://127.0.0.1:18080")


def test_a_spoolman_on_loopback_is_reached_without_any_test_exemption(monkeypatch):
    from core.config import settings
    from core.spoolman_client import fetch_spools

    spool = {"id": 7, "remaining_weight": 500, "comment": "UPSTREAM-ONLY",
             "filament": {"name": "PLA", "material": "PLA", "vendor": {"name": "V"}}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps([spool]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(settings, "spoolman_url", f"http://127.0.0.1:{server.server_address[1]}")
        spools = fetch_spools(timeout=5)
        assert [s["id"] for s in spools] == [7]
        assert "UPSTREAM-ONLY" not in json.dumps(spools)  # still projected, never the raw body
    finally:
        server.shutdown()
        server.server_close()


def test_saving_localhost_spoolman_is_200(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.config import settings
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_config
    application = FastAPI()
    application.include_router(routes_config.router, prefix="/api")
    monkeypatch.setattr(settings, "spoolman_url", "")
    monkeypatch.delenv("SPOOLMAN_URL", raising=False)
    saved = {}
    monkeypatch.setattr(routes_config, "set_spoolman_url", lambda db, url: saved.setdefault("url", url))
    monkeypatch.setattr(routes_config, "log_audit", lambda *a, **k: None)

    class _DB:
        def commit(self):
            pass
    application.dependency_overrides[get_db] = lambda: _DB()
    application.dependency_overrides[get_current_user] = lambda: {"id": 1, "role": "admin", "group_id": None, "is_active": True}
    response = TestClient(application).put("/api/config", json={"spoolman_url": "http://localhost:7912"})
    assert response.status_code == 200, response.text
    assert saved["url"] == "http://localhost:7912"
