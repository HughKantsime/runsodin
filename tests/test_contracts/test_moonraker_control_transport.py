"""Exercise device_urlopen, urllib and HTTP parsing with a local fake Moonraker."""
import errno
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from core import webhook_utils
from modules.printers.adapters import moonraker


@pytest.fixture
def peer(monkeypatch):
    requests, release = [], threading.Event()
    server = SimpleNamespace(code=200)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers.get("Content-Length"), self.headers.get("Content-Type")))
            if server.code in ("close", "reset"):
                if server.code == "reset":
                    import struct
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.close_connection = True
                return
            if server.code is None:
                release.wait(8)
                return
            self.send_response(server.code)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Printer is busy"}}')

        def log_message(self, *_):
            pass

    # Only this synthetic host is granted a loopback pin in the test.
    def resolve(host, port):
        assert host == "moonraker.test"
        return ["127.0.0.1"]
    monkeypatch.setattr(webhook_utils, "device_destination_ips", resolve)
    threads = []
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        # Sandboxes can deny bind(2). Keep the real HTTP exchange and the
        # five-second socket read timeout, using a connected local socket pair.
        httpd = None
        class LocalSocket(socket.socket):
            def recv_into(self, *args):
                size = super().recv_into(*args)
                if not size and server.code == "reset":
                    # AF_UNIX reports EOF even for an abortive peer close.
                    # Supply the TCP errno at the same real response-read boundary.
                    raise ConnectionResetError(errno.ECONNRESET, "peer reset")
                return size

            def setsockopt(self, level, option, value):
                if (level, option) != (socket.IPPROTO_TCP, socket.TCP_NODELAY):
                    super().setsockopt(level, option, value)
        def connect(address, timeout, *_):
            assert address[0] == "moonraker.test"
            client, remote = socket.socketpair()
            client = LocalSocket(fileno=client.detach())
            client.settimeout(timeout)
            def serve():
                with remote:
                    Handler(remote, ("local", 0), server)
            thread = threading.Thread(target=serve)
            threads.append(thread)
            thread.start()
            return client
        monkeypatch.setattr(socket, "create_connection", connect)
    else:
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        threads.append(thread)
    yield moonraker.MoonrakerPrinter("moonraker.test", httpd.server_port if httpd else 80), server, requests
    release.set()
    if httpd:
        httpd.shutdown()
        httpd.server_close()
    for thread in threads:
        thread.join(10)


@pytest.mark.parametrize("code,outcome", [(200, "accepted"), (409, "rejected"), (503, "rejected"), (None, "no_reply")])
def test_control_post(peer, code, outcome):
    adapter, server, requests = peer
    server.code = code
    start = time.monotonic()
    reply = adapter.post_control("stop")
    assert (reply.outcome, reply.status) == (outcome, code)
    assert requests == [("/printer/print/cancel", "0", "application/json")]
    if code in (409, 503):
        assert reply.error == "Printer is busy"
    if code is None:
        assert 4.8 <= time.monotonic() - start < 7


@pytest.mark.parametrize("reason", [ConnectionRefusedError(errno.ECONNREFUSED, "refused"), TimeoutError()])
def test_connection_errors_in_pinned_opener_are_unreachable(monkeypatch, reason):
    monkeypatch.setattr(webhook_utils, "device_destination_ips", lambda *_: ["127.0.0.1"])
    def refuse(*_):
        raise reason
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert moonraker.MoonrakerPrinter("moonraker.test").post_control("pause").outcome == "unreachable"


def test_blocked_destination_is_unreachable():
    assert moonraker.MoonrakerPrinter("127.0.0.1").post_control("pause").outcome == "unreachable"


def test_local_tcp_port_refuses_control(monkeypatch):
    with socket.socket() as reserved:
        try:
            reserved.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("TCP bind is sandboxed; connect exceptions are covered through the pinned opener")
        # The port is reserved but deliberately never listens.
        monkeypatch.setattr(webhook_utils, "device_destination_ips", lambda *_: ["127.0.0.1"])
        printer = moonraker.MoonrakerPrinter("moonraker.test", reserved.getsockname()[1])
        assert printer.post_control("pause").outcome == "unreachable"


@pytest.mark.parametrize("close", ["close", "reset"])
def test_peer_closes_after_reading_post(peer, close):
    adapter, server, requests = peer
    server.code = close
    assert adapter.post_control("stop").outcome == "no_reply"
    assert len(requests) == 1
