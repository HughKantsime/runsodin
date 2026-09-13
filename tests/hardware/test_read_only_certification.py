import inspect
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from modules.printers.adapters.elegoo import ElegooPrinter
from modules.printers.adapters.moonraker import MoonrakerPrinter, MoonrakerState
from modules.printers.adapters.prusalink import PrusaLinkPrinter, PrusaLinkState
from modules.printers.parsing.prusalink import parse_legacy_status
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
        def read(self, _limit=None): return b'{"ok": true}'
        headers = SimpleNamespace(get_content_type=lambda: "application/json")

    def fake_open(request, timeout):
        observed.append((request.get_method(), request.full_url, timeout))
        return Response()

    monkeypatch.setattr(
        "ops.hardware_certification.passive.transports._open_no_redirect",
        fake_open,
    )
    moon = ReadOnlyHttpTransport("http://printer.test", "moonraker")
    assert moon.get("/server/info") == {"ok": True}
    assert moon.get("/printer/objects/query?heater_bed&extruder") == {"ok": True}
    prusa = ReadOnlyHttpTransport("http://printer.test", "prusalink")
    assert prusa.get("/api/v1/status") == {"ok": True}
    assert all(method == "GET" for method, _, _ in observed)

    for call in (
        lambda: moon.get("/printer/gcode/script?script=HOME"),
        lambda: moon.get("/printer/objects/query?configfile"),
        lambda: prusa.get("/api/files"),
    ):
        with pytest.raises(CertificationMutationBlocked):
            call()


def test_mqtt_and_websocket_wrappers_cannot_send():
    network = FakeNetwork()
    mqtt = PassiveMqttTransport(network, "REDACTED")
    mqtt.connect("printer.test", 8883)
    mqtt.subscribe("device/REDACTED/report")
    assert not hasattr(mqtt, "publish")
    with pytest.raises(CertificationMutationBlocked):
        mqtt.subscribe("device/REDACTED/request")

    websocket = PassiveWebSocketTransport(network)
    assert websocket.receive() == "synthetic-status"
    assert not any(hasattr(websocket, name) for name in ("send", "send_text", "send_bytes", "send_json"))


def test_passive_http_capabilities_have_no_mutation_or_raw_connection_surface():
    for transport in (
        ReadOnlyHttpTransport("http://printer.test", "moonraker"),
        ReadOnlyHttpTransport("http://printer.test", "prusalink"),
    ):
        assert not any(
            hasattr(transport, name)
            for name in ("post", "put", "patch", "delete", "head", "request", "_connection")
        )


def test_prusalink_legacy_unknown_flags_remain_offline_and_ready_is_explicit():
    unknown = parse_legacy_status({"state": {"flags": {"future": True}}}, {})
    assert unknown["state"] == "DISCONNECTED"
    assert unknown["internal_state"] == "OFFLINE"
    ready = parse_legacy_status({"state": {"flags": {"operational": True}}}, {})
    assert ready["state"] == "IDLE"
    assert ready["internal_state"] == "IDLE"


def test_elegoo_passive_frame_requires_unsolicited_telemetry_topic():
    frame = (FIXTURES / "elegoo-status.json").read_text()
    assert verify_live.validate_elegoo_passive_frame(frame) == "status"
    with pytest.raises(ValueError, match="not unsolicited"):
        verify_live.validate_elegoo_passive_frame('{"ok": true}')
    with pytest.raises(ValueError, match="valid JSON"):
        verify_live.validate_elegoo_passive_frame("not-json")


def test_legacy_live_probe_is_retired_without_network_or_credentials():
    status, findings, metrics = verify_live._probe_hardware(
        "hardware_elegoo_live", "printer.test"
    )
    assert status == "blocked"
    assert "retired" in findings[0]
    assert metrics["certification_level"] == "none"


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


@pytest.mark.parametrize("gate_id", ["hardware_moonraker_live", "hardware_prusalink_live"])
def test_legacy_http_probe_does_not_execute_network(gate_id):
    status, findings, _metrics = verify_live._probe_hardware(gate_id, "http://printer.test")
    assert status == "blocked"
    assert "ops.hardware_certification observe" in findings[0]
