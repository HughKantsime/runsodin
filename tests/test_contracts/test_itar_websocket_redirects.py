"""Under ITAR mode the Elegoo WebSocket follows no redirect away from the checked address.

Review 2026-11-13 finding 3: the adapter and the pre-dispatch status read connect to the
address that passed the device policy, but websocket-client 1.9.2 follows up to three HTTP
redirects by itself. A printer on the local network that answers
`302 Location: ws://<public address>:3030/websocket` made ODIN connect there unchecked.

Two real listeners on this machine's loopback stand in for the printer and for the address
it redirects to; the redirect target counts the connections it receives. device_connect_ip
is replaced only so that the loopback stub is accepted as the printer's address (the device
policy refuses loopback). No other address is contacted.
"""
import socket
import threading
import time

import pytest

from core import webhook_utils
from modules.printers import dispatch_readiness, sdcp_websocket
from modules.printers.adapters.elegoo import ElegooPrinter


class Listener:
    """Accepts connections on 127.0.0.1, counts them, and answers each with `reply`."""

    def __init__(self, reply=b""):
        self.reply, self.accepted = reply, 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                conn.settimeout(2)
                conn.recv(4096)
                reply = self.reply() if callable(self.reply) else self.reply
                if reply:
                    conn.sendall(reply)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def printer_that_redirects(monkeypatch):
    target = Listener()
    printer = Listener(lambda: (f"HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:{target.port}/websocket\r\n"
                                "Content-Length: 0\r\n\r\n").encode())
    monkeypatch.setattr(webhook_utils, "device_connect_ip", lambda host, port=None: "127.0.0.1")
    yield printer, target
    printer.close()
    target.close()


def _settle(listener, expected):
    deadline = time.monotonic() + 3
    while listener.accepted < expected and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)


def _status_read(printer):
    creds = {"ip": "printer.plant.example", "port": printer.port, "api_key": "BOARD1"}
    return dispatch_readiness.fresh_sdcp_status_is_idle(creds, time.monotonic() + 3)


def test_the_status_read_follows_no_redirect_under_itar(printer_that_redirects, monkeypatch):
    printer, target = printer_that_redirects
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    assert _status_read(printer) is False
    _settle(printer, 1)
    assert printer.accepted == 1
    assert target.accepted == 0, "ODIN connected to the address the printer redirected to"


def test_the_adapter_follows_no_redirect_under_itar(printer_that_redirects, monkeypatch):
    printer, target = printer_that_redirects
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    client = ElegooPrinter("printer.plant.example", port=printer.port, mainboard_id="BOARD1")
    assert client.connect() is False
    _settle(printer, 1)
    assert printer.accepted == 1
    assert target.accepted == 0, "ODIN connected to the address the printer redirected to"


def test_with_itar_off_the_library_default_is_unchanged(printer_that_redirects, monkeypatch):
    """ITAR mode off: nothing is passed to the library, so it behaves as in 2.2.0 (it follows
    the redirect). This pins "no behaviour change with ITAR mode off"."""
    printer, target = printer_that_redirects
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    assert sdcp_websocket.connection_options() == {}
    assert _status_read(printer) is False
    _settle(target, 1)
    assert printer.accepted == 1 and target.accepted == 1
    client = ElegooPrinter("printer.plant.example", port=printer.port, mainboard_id="BOARD1")
    assert client.connect() is False
    _settle(target, 2)
    assert printer.accepted == 2 and target.accepted == 2


def test_the_library_still_has_the_names_the_setting_relies_on():
    """websocket-client is pinned to 1.9.2. If an upgrade changes these, ITAR mode refuses the
    Elegoo connection (sdcp_websocket.websocket_app) and this test says why."""
    import inspect
    import websocket
    import websocket._app
    import websocket._core
    assert 'options.pop("redirect_limit", 3)' in inspect.getsource(websocket._core.WebSocket.connect)
    sdcp_websocket._make_app_sockets_check_itar()
    assert websocket._app.WebSocket._odin_checks_itar is True
    assert issubclass(websocket._app.WebSocket, websocket._core.WebSocket)
    assert "self.sock = WebSocket(" in inspect.getsource(websocket._app.WebSocketApp.run_forever)


def test_under_itar_a_library_without_those_names_is_refused(monkeypatch):
    def broken():
        raise ImportError("websocket._app")
    monkeypatch.setattr(sdcp_websocket, "_make_app_sockets_check_itar", broken)
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(ImportError):
        sdcp_websocket.websocket_app("ws://10.0.0.5:3030/websocket")
    monkeypatch.delenv("ODIN_ITAR_MODE")
    assert sdcp_websocket.websocket_app("ws://10.0.0.5:3030/websocket") is not None
