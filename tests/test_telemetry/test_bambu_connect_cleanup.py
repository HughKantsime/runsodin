"""A failed Bambu connect must stop paho's background loop (it reconnects forever)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from modules.printers.adapters import bambu as bambu_adapter


class FakeClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.loop_started = self.loop_stopped = self.disconnected = False
        self.sock = None
        self.on_connect = None
        FakeClient.instances.append(self)

    def username_pw_set(self, *a): pass
    def tls_set(self, **k): pass
    def tls_insecure_set(self, *a): pass

    def connect(self, *a, **k):
        if FakeClient.connect_error:
            raise FakeClient.connect_error

    def subscribe(self, *a, **k): return (0, 1)
    def publish(self, *a, **k):
        class Info: rc = 0
        return Info()
    def socket(self): return self.sock

    def loop_start(self):
        self.loop_started = True
        if FakeClient.ack_on_start:
            self.on_connect(self, None, None, 0)
    def loop_stop(self):
        self.loop_stopped = True
        if FakeClient.reconnect_during_stop:
            self.sock = FakeSocket()  # paho finished a reconnect while joining
    def disconnect(self): self.disconnected = True


def _printer(monkeypatch, error=None, ack=False):
    FakeClient.instances = []
    FakeClient.connect_error = error
    FakeClient.ack_on_start = ack
    FakeClient.reconnect_during_stop = False
    monkeypatch.setattr(bambu_adapter.mqtt, "Client", FakeClient)
    monkeypatch.setattr("core.webhook_utils.device_connect_ip", lambda host, port: host)
    printer = bambu_adapter.BambuPrinter(ip="192.0.2.10", serial="SERIAL01", access_code="code")
    printer.MQTT_TIMEOUT = 0.2
    return printer


def test_timed_out_connect_stops_the_loop(monkeypatch):
    printer = _printer(monkeypatch)
    assert printer.connect() is False
    client = FakeClient.instances[-1]
    assert client.loop_started and client.loop_stopped and client.disconnected
    assert printer._client is None and printer._connected is False


def test_connect_error_leaves_no_client(monkeypatch):
    printer = _printer(monkeypatch, error=TimeoutError("handshake"))
    assert printer.connect() is False
    client = FakeClient.instances[-1]
    assert not client.loop_started and client.loop_stopped and client.disconnected
    assert printer._client is None and printer._connected is False


def test_repeated_failures_leave_no_running_loops(monkeypatch):
    printer = _printer(monkeypatch)
    for _ in range(3):
        assert printer.connect() is False
    assert all(c.loop_stopped for c in FakeClient.instances if c.loop_started)
    printer.disconnect()  # safe after a failed connect


class FakeSocket:
    closed = False
    def close(self): self.closed = True


def test_a_successful_connect_keeps_its_client(monkeypatch):
    printer = _printer(monkeypatch, ack=True)
    assert printer.connect() is True
    client = FakeClient.instances[-1]
    assert printer._client is client and not client.loop_stopped
    printer.disconnect()
    assert client.loop_stopped


def test_failure_then_retry_succeeds(monkeypatch):
    printer = _printer(monkeypatch)
    assert printer.connect() is False
    FakeClient.ack_on_start = True
    assert printer.connect() is True
    first, second = FakeClient.instances
    assert first.loop_stopped and printer._client is second


def test_reconnect_finishing_during_cleanup_is_closed_and_fenced(monkeypatch):
    printer = _printer(monkeypatch)
    FakeClient.reconnect_during_stop = True
    assert printer.connect() is False
    abandoned = FakeClient.instances[-1]
    assert abandoned.sock is not None and abandoned.sock.closed
    # A late CONNACK from the abandoned client must not mark the adapter live.
    printer._on_connect(abandoned, None, None, 0)
    assert printer._connected is False and printer._client is None
