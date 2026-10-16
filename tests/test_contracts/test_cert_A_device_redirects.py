"""Group A: device HTTP clients never follow redirects or use environment proxies.

A real device double on 127.0.0.1 answers every request with a 302 to
another path on itself. The pin allows that host and port, so only the
client's redirect policy stops the second request. HTTP_PROXY/HTTPS_PROXY
point at the same server: a proxied request arrives in absolute form
("GET http://...") and is recorded as a proxy use.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def device(monkeypatch, tmp_path):
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append((self.command, self.path))
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    upload = tmp_path / "part.gcode"
    upload.write_text("G28\n")
    yield port, seen, upload
    server.shutdown()
    server.server_close()


def assert_single_direct_request(seen, method, path):
    assert seen, "device was not contacted"
    assert seen == [(method, path)], seen  # no redirect follow-up, no absolute-form proxy request


def test_moonraker_get_and_post(device):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    port, seen, _ = device
    printer = MoonrakerPrinter("127.0.0.1", port=port)
    assert printer._get("/printer/info") is None
    assert_single_direct_request(seen, "GET", "/printer/info")
    seen.clear()
    assert printer._post("/printer/print/pause") is None
    assert_single_direct_request(seen, "POST", "/printer/print/pause")


def test_moonraker_upload_file(device):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    port, seen, upload = device
    assert MoonrakerPrinter("127.0.0.1", port=port).upload_file(str(upload)) is False
    assert_single_direct_request(seen, "POST", "/server/files/upload")


def test_prusalink_get(device):
    from modules.printers.adapters.prusalink import PrusaLinkPrinter
    port, seen, _ = device
    assert PrusaLinkPrinter("127.0.0.1", port=port, api_key="synthetic")._get("/api/v1/status") is None
    assert_single_direct_request(seen, "GET", "/api/v1/status")


def test_dispatch_readiness(device):
    import time
    from modules.printers.dispatch_readiness import fresh_http_status_is_idle
    port, seen, _ = device
    creds = {"api_type": "prusalink", "ip": "127.0.0.1", "port": port, "api_key": "synthetic"}
    assert fresh_http_status_is_idle(creds, time.monotonic() + 5) is False
    assert_single_direct_request(seen, "GET", "/api/v1/status")


def test_smart_plug_http(device):
    from modules.printers import plug_http_protocol
    port, seen, _ = device
    status, _ = plug_http_protocol.http_request(f"http://127.0.0.1:{port}/cm?cmnd=Power", timeout=2)
    assert status == 302
    assert_single_direct_request(seen, "GET", "/cm?cmnd=Power")


def test_test_connection_httpx_probe(device):
    import httpx
    from modules.printers.route_utils import pinned_device_get
    port, seen, _ = device
    response = pinned_device_get(httpx, f"http://127.0.0.1:{port}/printer/info", timeout=2)
    assert response.status_code == 302
    assert_single_direct_request(seen, "GET", "/printer/info")


def test_elegoo_camera_probe_uses_device_request(monkeypatch):
    from types import SimpleNamespace
    from modules.printers.adapters.elegoo import ElegooPrinter
    from tests.support.device_http import route_device_requests
    calls = []
    route_device_requests(monkeypatch, head=lambda url, **kw: calls.append(url) or SimpleNamespace(status_code=404))
    assert ElegooPrinter("192.0.2.11").get_webcam_url() is None
    assert len(calls) == 2  # route_device_requests asserts no redirects and no env proxies
