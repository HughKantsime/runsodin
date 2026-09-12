import inspect
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from modules.printers.adapters.elegoo import ElegooPrinter
from modules.printers.adapters.moonraker import MoonrakerPrinter, MoonrakerState
from modules.printers.adapters.prusalink import PrusaLinkPrinter, PrusaLinkState
from ops.edu_readiness.hardware_probe import (
    CertificationMutationBlocked,
    PassiveMqttTransport,
    PassiveWebSocketTransport,
    ReadOnlyHttpTransport,
    redact_observation,
)
from ops.edu_readiness import verify_live

FIXTURES = Path(__file__).parent / "fixtures"


class FakeNetwork:
    def __init__(self):
        self.calls = []

    def connect(self, *args, **kwargs):
        self.calls.append(("connect", args, kwargs))

    def subscribe(self, *args, **kwargs):
        self.calls.append(("subscribe", args, kwargs))

    def disconnect(self):
        self.calls.append(("disconnect", (), {}))

    def recv(self):
        return "synthetic-status"

    def close(self):
        self.calls.append(("close", (), {}))


def test_http_transport_enforces_exact_get_allowlists(monkeypatch):
    observed = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return b'{"ok": true}'

    def fake_open(request, timeout):
        observed.append((request.get_method(), request.full_url, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    moon = ReadOnlyHttpTransport("http://printer.test", "moonraker")
    assert moon.get("/server/info") == {"ok": True}
    assert moon.get("/printer/objects/query?heater_bed&extruder") == {"ok": True}
    prusa = ReadOnlyHttpTransport("http://printer.test", "prusalink")
    assert prusa.get("/api/v1/status") == {"ok": True}
    assert all(method == "GET" for method, _, _ in observed)

    for call in (
        lambda: moon.post("/printer/print/start"),
        lambda: moon.get("/printer/gcode/script?script=HOME"),
        lambda: moon.get("/printer/objects/query?configfile"),
        lambda: prusa.put("/api/v1/job/1/pause"),
        lambda: prusa.get("/api/files"),
    ):
        with pytest.raises(CertificationMutationBlocked):
            call()


def test_mqtt_and_websocket_wrappers_cannot_send():
    network = FakeNetwork()
    mqtt = PassiveMqttTransport(network, "REDACTED")
    mqtt.connect("printer.test", 8883)
    mqtt.subscribe("device/REDACTED/report")
    with pytest.raises(CertificationMutationBlocked):
        mqtt.publish("device/REDACTED/request", "pushall")
    with pytest.raises(CertificationMutationBlocked):
        mqtt.subscribe("device/REDACTED/request")

    websocket = PassiveWebSocketTransport(network)
    assert websocket.receive() == "synthetic-status"
    with pytest.raises(CertificationMutationBlocked):
        websocket.send_json({"Cmd": 128})


def test_elegoo_passive_frame_requires_unsolicited_telemetry_topic():
    frame = (FIXTURES / "elegoo-status.json").read_text()
    assert verify_live.validate_elegoo_passive_frame(frame) == "status"
    with pytest.raises(ValueError, match="not unsolicited"):
        verify_live.validate_elegoo_passive_frame('{"ok": true}')
    with pytest.raises(ValueError, match="valid JSON"):
        verify_live.validate_elegoo_passive_frame("not-json")


def test_elegoo_live_probe_rejects_arbitrary_json(monkeypatch):
    class FakeSocket:
        def recv(self):
            return '{"ok": true}'

        def close(self):
            return None

        def send(self, *_args, **_kwargs):
            pytest.fail("read-only certification probe attempted to send")

    monkeypatch.setitem(
        sys.modules,
        "websocket",
        SimpleNamespace(create_connection=lambda *_args, **_kwargs: FakeSocket()),
    )
    status, findings, metrics = verify_live._probe_hardware(
        "hardware_elegoo_live", "printer.test"
    )
    assert status == "fail"
    assert findings == ["passive observation failed (ValueError)"]
    assert metrics == {"endpoint_configured": True}


def test_protocol_fixtures_drive_real_parsers(monkeypatch):
    moon_payload = json.loads((FIXTURES / "moonraker-status.json").read_text())
    moon = MoonrakerPrinter("printer.test")
    monkeypatch.setattr(moon, "_get", lambda _path: moon_payload)
    moon_status = moon.get_status()
    assert moon_status.state == MoonrakerState.READY
    assert moon_status.bed_temp == 24.5

    prusa_payload = json.loads((FIXTURES / "prusalink-status.json").read_text())
    prusa = PrusaLinkPrinter("printer.test")
    monkeypatch.setattr(prusa, "_get", lambda _path: prusa_payload)
    prusa_status = prusa.get_status()
    assert prusa_status.state == PrusaLinkState.PRINTING
    assert prusa_status.progress_percent == 25.0

    elegoo_payload = (FIXTURES / "elegoo-status.json").read_text()
    assert verify_live.validate_elegoo_passive_frame(elegoo_payload) == "status"
    elegoo = ElegooPrinter("printer.test")
    elegoo._on_message(None, elegoo_payload)
    elegoo_status = elegoo.get_status()
    assert elegoo_status.internal_state == "PRINTING"
    assert elegoo_status.time_remaining == 900


@pytest.mark.parametrize(
    "frame",
    [
        '{"ok": true}',
        '[]',
        '{"Topic": "sdcp/request/device"}',
        "not-json",
    ],
)
def test_elegoo_passive_frame_rejects_unrecognized_or_non_telemetry_json(frame):
    with pytest.raises(ValueError):
        verify_live.validate_elegoo_passive_frame(frame)


def test_report_redaction_and_no_raw_socket_surface():
    result = redact_observation(
        "moonraker",
        {
            "host": "printer.test",
            "api_key": "never-record-this",
            "serial": "never-record-this",
            "firmware_version": "1.2.3",
            "capabilities": ["status"],
            "status": "pass",
        },
    )
    assert result == {
        "protocol": "moonraker",
        "firmware_version": "1.2.3",
        "capabilities": ["status"],
        "status": "pass",
    }
    source = inspect.getsource(__import__("ops.edu_readiness.hardware_probe", fromlist=["x"]))
    assert "import socket" not in source
    assert "sendto(" not in source


@pytest.mark.parametrize(
    "gate_id,expected_paths",
    [
        (
            "hardware_moonraker_live",
            ["/server/info", "/printer/info", "/printer/objects/list", "/printer/objects/query?print_stats"],
        ),
        (
            "hardware_prusalink_live",
            ["/api/version", "/api/v1/status", "/api/printer", "/api/job"],
        ),
    ],
)
def test_configured_http_live_probe_executes_only_allowlisted_gets(monkeypatch, gate_id, expected_paths):
    calls = []

    class FakeReadOnlyTransport:
        def __init__(self, endpoint, protocol):
            assert endpoint == "http://printer.test"
            assert protocol in {"moonraker", "prusalink"}

        def get(self, path):
            calls.append(path)
            return {"result": {"synthetic": True}}

    monkeypatch.setattr(verify_live, "ReadOnlyHttpTransport", FakeReadOnlyTransport)
    status, findings, metrics = verify_live._probe_hardware(gate_id, "http://printer.test")
    assert status == "pass"
    assert findings == []
    assert calls == expected_paths
    assert metrics["latency_ms"] >= 0
