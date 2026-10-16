"""Camera-stack review LOW 8: the relay checks the body is the declared MJPEG/JPEG before
streaming anything to go2rtc's ffmpeg (which would otherwise probe whatever it is sent,
e.g. an HLS playlist pointing elsewhere)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from tests.test_contracts.test_cert_A_camera_relay import _set_camera, relay  # noqa: F401
from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    bind_engine, cert_app, cert_client, fernet_env, review_db,
)

RESPONSES = {
    "/hls": ("multipart/x-mixed-replace; boundary=frame", b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttp://192.0.2.9/x.m3u8\n"),
    "/html": ("image/jpeg", b"<html>not a jpeg</html>"),
    "/wrong-boundary": ("multipart/x-mixed-replace; boundary=frame", b"--other\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8x"),
    "/jpeg": ("image/jpeg", b"\xff\xd8\xff\xe0synthetic\xff\xd9"),
    "/mjpeg": ("multipart/x-mixed-replace; boundary=frame",
               b"\r\n--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n"),
}


@pytest.fixture
def fake_camera():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            content_type, body = RESPONSES[self.path]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("path,status", [("/hls", 502), ("/html", 502), ("/wrong-boundary", 502),
                                         ("/jpeg", 200), ("/mjpeg", 200)])
def test_relay_requires_a_jpeg_soi_or_the_declared_multipart_boundary(relay, fake_camera, review_db, path, status):
    from modules.printers.camera_relay import relay_url
    _set_camera(review_db, f"http://127.0.0.1:{fake_camera}{path}")
    response = requests.get(relay_url(301), timeout=5)
    assert response.status_code == status
    if status == 200:
        assert response.content == RESPONSES[path][1]  # sniffed bytes are passed through intact
