"""Group A: internal pinned relay for HTTP/MJPEG cameras (go2rtc -> ffmpeg -> ODIN -> camera).

A real fake MJPEG camera and a real ODIN relay (uvicorn on 127.0.0.1) are
used end to end. The camera double runs on 127.0.0.1, so exactly that
address is exempted from the device policy here (tests/support); a redirect
to another loopback listener must still never be followed.
"""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    bind_engine, cert_app, cert_client, fernet_env, review_db,
)

BOUNDARY = b"frame"
FRAMES = [b"\xff\xd8synthetic-jpeg-%d\xff\xd9" % i for i in range(3)]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def listener():
    hits = []
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(0.1)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            hits.append(True)
            conn.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield sock.getsockname()[1], hits
    stop.set()
    thread.join(1)
    sock.close()


@pytest.fixture
def camera(listener):
    """Fake MJPEG camera: /stream streams frames, /moved redirects to the loopback listener."""
    target_port, _ = listener
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            if self.path.startswith("/moved"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/admin")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            for frame in FRAMES:
                self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame))
                self.wfile.write(frame + b"\r\n")
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def relay(cert_app, review_db, fernet_env, monkeypatch, tmp_path):
    """Real ODIN relay router served by uvicorn on 127.0.0.1."""
    import uvicorn
    from fastapi import FastAPI
    from modules.printers import camera_relay
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
    bind_engine(monkeypatch, review_db.get_bind())
    port = _free_port()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    camera_relay.rotate_relay_secret()
    app = FastAPI()
    app.include_router(camera_relay.router)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    yield app
    server.should_exit = True
    thread.join(5)


def _set_camera(review_db, url):
    import core.crypto as crypto
    updated = review_db.execute(text("UPDATE printers SET camera_url=:u,camera_enabled=1,is_active=1 WHERE id=301"),
                                {"u": crypto.encrypt(url)})
    review_db.commit()
    assert updated.rowcount == 1


def test_mjpeg_stream_is_relayed_end_to_end(relay, camera, review_db):
    from modules.printers.camera_relay import relay_url
    port, seen = camera
    _set_camera(review_db, f"http://127.0.0.1:{port}/stream")
    response = requests.get(relay_url(301), timeout=5, stream=True)
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
    body = b"".join(response.iter_content(4096))
    assert all(frame in body for frame in FRAMES) and body.count(b"--" + BOUNDARY) == 3
    assert seen == ["/stream"]


def test_camera_redirect_to_loopback_is_never_followed(relay, camera, listener, review_db):
    from modules.printers.camera_relay import relay_url
    port, seen = camera
    _, hits = listener
    _set_camera(review_db, f"http://127.0.0.1:{port}/moved")
    response = requests.get(relay_url(301), timeout=5, allow_redirects=False)
    assert response.status_code == 502 and "location" not in {k.lower() for k in response.headers}
    time.sleep(0.2)
    assert seen == ["/moved"] and hits == []


def test_camera_host_rebound_to_another_loopback_port_is_not_reached(relay, listener, review_db, monkeypatch):
    """Without the exemption, a camera name resolving to loopback is refused by the policy."""
    import core.webhook_utils as webhook_utils
    from modules.printers.camera_relay import relay_url
    port, hits = listener
    _set_camera(review_db, f"http://rebound-camera.test:{port}/stream")
    real = webhook_utils._real_getaddrinfo
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, p, *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", p or 0))]
                        if webhook_utils.normalize_hostname(host) == "rebound-camera.test" else real(host, p, *a, **k))
    assert requests.get(relay_url(301), timeout=5).status_code == 502
    assert hits == []


@pytest.mark.parametrize("client,headers,token_ok,expected", [
    (("203.0.113.9", 4000), {}, True, 404),                          # external peer
    (("172.17.0.1", 4000), {}, True, 404),                           # docker bridge peer
    (("127.0.0.1", 4000), {"X-Forwarded-For": "203.0.113.9"}, True, 404),  # proxied from outside
    (("127.0.0.1", 4000), {"Forwarded": "for=203.0.113.9"}, True, 404),
    (("127.0.0.1", 4000), {}, False, 404),                           # wrong (other printer's) token
    (("127.0.0.1", 4000), {}, True, 502),                            # control: authorised, camera host blocked
])
def test_internal_endpoint_refuses_external_or_unauthenticated_requests(cert_app, review_db, fernet_env, monkeypatch, tmp_path,
                                                                        client, headers, token_ok, expected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.printers import camera_relay
    bind_engine(monkeypatch, review_db.get_bind())
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    camera_relay.rotate_relay_secret()
    _set_camera(review_db, "http://169.254.169.254/stream")  # never fetched: blocked by the device policy
    app = FastAPI()
    app.include_router(camera_relay.router)
    token = camera_relay.relay_token(301) if token_ok else camera_relay.relay_token(302)
    response = TestClient(app, client=client).get(f"/internal/camera-source/301/{token}", headers=headers,
                                                   follow_redirects=False)
    assert response.status_code == expected


def test_secret_rotates_per_boot_and_tokens_are_per_printer(monkeypatch, tmp_path):
    import os
    from modules.printers import camera_relay
    path = tmp_path / "relay-secret"
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(path))
    camera_relay.rotate_relay_secret()
    first = camera_relay.relay_token(7)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert camera_relay.relay_token(7) == first != camera_relay.relay_token(8)
    camera_relay.rotate_relay_secret()  # next boot
    assert camera_relay.relay_token(7) != first


def test_relay_route_is_registered_outside_the_api_and_on_the_census_allowlist():
    from tests.test_contracts.test_route_auth_census import PUBLIC
    assert "GET /internal/camera-source/{printer_id}/{token}" in PUBLIC
