"""Contract tests for BambuTelemetryAdapter (T4.1, T4.2, T4.3).

Tests use a FakeMqttClient that captures calls and fires callbacks
manually — no real broker is spawned. Integration tests against a real
Bambu broker are out of scope for CI (legacy path kept those tests
commented-out).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.modules.printers.telemetry.bambu.adapter import (
    BambuAdapterConfig,
    BambuTelemetryAdapter,
)
from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    TelemetryEvent,
)
from backend.modules.printers.telemetry.state import (
    PrinterState,
    StateTransitionEvent,
)


class FakeMqttClient:
    """Stand-in for paho.mqtt.client.Client — captures calls, exposes
    callback hooks that tests invoke to drive the adapter."""

    def __init__(self):
        self.tls_set_context = MagicMock()
        self.username_pw_set = MagicMock()
        self.connect_async = MagicMock()
        self.loop_start = MagicMock()
        self.loop_stop = MagicMock()
        self.disconnect = MagicMock()
        self.subscribe = MagicMock()

        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def fire_connect(self, reason_code=0):
        self.on_connect(self, None, {}, reason_code)

    def fire_disconnect(self):
        self.on_disconnect(self, None)

    def fire_message(self, topic: str, payload: bytes):
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload
        self.on_message(self, None, msg)


@pytest.fixture
def config():
    return BambuAdapterConfig(
        printer_id="h2d-01",
        serial="0948AD561201838",
        host="192.168.1.42",
        access_code="SECRET",
    )


@pytest.fixture
def captured_events():
    return []


@pytest.fixture
def adapter(config, captured_events, monkeypatch):
    """Adapter with FakeMqttClient + list-capture emitter."""
    fake = FakeMqttClient()
    monkeypatch.setattr(
        BambuTelemetryAdapter, "_client_factory", staticmethod(lambda: fake),
    )
    adapter = BambuTelemetryAdapter(config, emitter=captured_events.append)
    adapter._fake = fake  # convenience handle for tests
    return adapter


class TestLifecycle:
    def test_start_subscribes(self, adapter, config):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        adapter._fake.subscribe.assert_called_once_with(config.topic_report, qos=0)

    def test_start_twice_raises(self, adapter):
        adapter.start()
        with pytest.raises(RuntimeError, match="already started"):
            adapter.start()

    def test_stop_disconnects(self, adapter):
        adapter.start()
        adapter.stop()
        adapter._fake.disconnect.assert_called_once()
        adapter._fake.loop_stop.assert_called_once()

    def test_stop_before_start_is_noop(self, config, captured_events):
        adapter = BambuTelemetryAdapter(config, captured_events.append)
        adapter.stop()  # must not raise


class TestConnectionEvents:
    def test_connect_success_emits_connection_event(self, adapter, captured_events):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        connection_events = [e for e in captured_events if isinstance(e, ConnectionEvent)]
        assert len(connection_events) == 1
        assert connection_events[0].kind == "connected"

    def test_connect_failure_emits_error(self, adapter, captured_events):
        adapter.start()
        adapter._fake.fire_connect(reason_code=5)  # nonzero = failure
        connection_events = [e for e in captured_events if isinstance(e, ConnectionEvent)]
        assert len(connection_events) == 1
        assert connection_events[0].kind == "error"

    def test_disconnect_emits_event(self, adapter, captured_events):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        adapter._fake.fire_disconnect()
        events = [e for e in captured_events if isinstance(e, ConnectionEvent)]
        kinds = [e.kind for e in events]
        assert "connected" in kinds
        assert "disconnected" in kinds


class TestReportIngestion:
    def test_valid_push_status_emits_event_and_transition(self, adapter, captured_events):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)

        payload = json.dumps({
            "print": {
                "gcode_state": "RUNNING",
                "stg_cur": 14,
                "mc_percent": 50,
            }
        }).encode()
        adapter._fake.fire_message("device/0948AD561201838/report", payload)

        # Expected events: connection, BambuReportEvent, at least one transition
        report_events = [e for e in captured_events if isinstance(e, BambuReportEvent)]
        transitions = [e for e in captured_events if isinstance(e, StateTransitionEvent)]
        assert len(report_events) == 1
        assert report_events[0].section.gcode_state == "RUNNING"
        # 2 transitions: offline→idle (from connected event) + idle→printing (from report)
        assert len(transitions) == 2
        assert transitions[-1].to_state == PrinterState.PRINTING

    def test_info_payload_emits_info_event(self, adapter, captured_events):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)

        payload = json.dumps({
            "info": {
                "command": "get_version",
                "module": [
                    {"name": "ota", "sw_ver": "01.09.00.00"},
                ],
            }
        }).encode()
        adapter._fake.fire_message("device/xxx/report", payload)

        info_events = [e for e in captured_events if isinstance(e, BambuInfoEvent)]
        assert len(info_events) == 1

    def test_adapter_updates_status(self, adapter):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        payload = json.dumps({
            "print": {"gcode_state": "RUNNING", "stg_cur": 14, "mc_percent": 50},
        }).encode()
        adapter._fake.fire_message("device/xxx/report", payload)

        status = adapter.status()
        assert status.state == PrinterState.PRINTING
        assert status.progress_percent == 50

    def test_failed_payload_yields_failed_state(self, adapter, captured_events):
        """The headline legacy bug — FAILED must surface, not collapse to IDLE."""
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        adapter._fake.fire_message(
            "device/xxx/report",
            json.dumps({"print": {"gcode_state": "RUNNING", "stg_cur": 14}}).encode(),
        )
        adapter._fake.fire_message(
            "device/xxx/report",
            json.dumps({"print": {"gcode_state": "FAILED"}}).encode(),
        )
        assert adapter.status().state == PrinterState.FAILED
        assert adapter.status().state != PrinterState.IDLE


class TestFailLoudParsing:
    def test_bad_json_emits_degraded_event(self, adapter, captured_events):
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        adapter._fake.fire_message("device/xxx/report", b"not valid json{{{")

        degraded = [e for e in captured_events if isinstance(e, DegradedEvent)]
        assert len(degraded) == 1
        assert adapter.status().state == PrinterState.DEGRADED

    def test_invalid_shape_emits_degraded(self, adapter, captured_events):
        """Payload with neither print nor info is a suspicious shape — fail loud
        to DEGRADED state, not silent skip. Legacy's `except Exception: pass`
        hid drifts like this indefinitely."""
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)

        adapter._fake.fire_message(
            "device/xxx/report",
            json.dumps({"heartbeat": True}).encode(),
        )
        degraded = [e for e in captured_events if isinstance(e, DegradedEvent)]
        assert len(degraded) == 1
        assert "neither" in degraded[0].reason
        assert adapter.status().state == PrinterState.DEGRADED

    def test_unknown_gcode_state_emits_degraded(self, adapter, captured_events):
        """Unknown enum value → ValidationError → DegradedEvent.

        This replaces legacy silent UNKNOWN fallback."""
        adapter.start()
        adapter._fake.fire_connect(reason_code=0)
        adapter._fake.fire_message(
            "device/xxx/report",
            json.dumps({"print": {"gcode_state": "CRASHED_HARD"}}).encode(),
        )
        degraded = [e for e in captured_events if isinstance(e, DegradedEvent)]
        assert len(degraded) == 1
        assert "CRASHED_HARD" in degraded[0].raw_excerpt


class TestEmitterResilience:
    def test_emitter_exception_does_not_crash_adapter(self, config, monkeypatch):
        """Emitter raising must not crash the paho message handler."""
        fake = FakeMqttClient()
        monkeypatch.setattr(
            BambuTelemetryAdapter, "_client_factory", staticmethod(lambda: fake),
        )

        def bad_emitter(item):
            raise RuntimeError("emitter boom")

        adapter = BambuTelemetryAdapter(config, emitter=bad_emitter)
        adapter.start()
        fake.fire_connect(reason_code=0)
        # must not raise
        fake.fire_message(
            "device/xxx/report",
            json.dumps({"print": {"gcode_state": "RUNNING", "stg_cur": 14}}).encode(),
        )
        # Adapter status is still updated despite emitter failure
        assert adapter.status().state == PrinterState.PRINTING


class TestTopicDerivation:
    def test_topic_report(self, config):
        assert config.topic_report == "device/0948AD561201838/report"

    def test_topic_request(self, config):
        assert config.topic_request == "device/0948AD561201838/request"
