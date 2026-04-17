"""End-to-end tests for live MQTT replay (T3.2 + T3.4 + T3.6).

Spins up an embedded amqtt broker, publishes a fixture through the real
paho client, subscribes a real `BambuTelemetryAdapter`, asserts the
adapter produces the expected state transitions.

This closes the "does the whole adapter stack work on real MQTT?"
question that the in-process replay tests couldn't answer.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.bambu.adapter import (
    BambuAdapterConfig,
    BambuTelemetryAdapter,
)
from backend.modules.printers.telemetry.events import (
    BambuReportEvent,
)
from backend.modules.printers.telemetry.live_replay import (
    LocalBroker,
    publish_fixture,
)
from backend.modules.printers.telemetry.state import (
    PrinterState,
    StateTransitionEvent,
)


FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"


@pytest.fixture
def broker():
    b = LocalBroker()
    b.start()
    yield b
    b.stop()


def _wait_for(predicate, timeout=5.0, poll=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


class TestLocalBroker:
    def test_broker_lifecycle(self, broker):
        assert broker.port > 1024
        # Connect a paho client to prove broker is alive
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        rc = c.connect(broker.host, broker.port, keepalive=5)
        assert rc == 0
        c.disconnect()

    def test_double_start_raises(self, broker):
        with pytest.raises(RuntimeError, match="already started"):
            broker.start()

    def test_stop_is_idempotent(self):
        b = LocalBroker()
        b.stop()  # not started — should not raise


class TestPublishFixture:
    def test_publish_a1_kickoff(self, broker):
        result = publish_fixture(
            FIXTURES / "bambu-a1-kickoff.jsonl",
            broker.host, broker.port,
            serial="TEST-A1",
            speed=1000.0,  # very fast for test
        )
        assert result.messages_published > 0
        # Kickoff fixture has 34 lines; at least some are recv
        assert result.messages_published >= 4  # IDLE + PREPARE + RUNNING + ams
        assert result.duration_sec < 5.0  # even at speed=1000 on 34 lines

    def test_invalid_speed_raises(self, broker):
        with pytest.raises(ValueError):
            publish_fixture(
                FIXTURES / "bambu-a1-kickoff.jsonl",
                broker.host, broker.port,
                serial="x",
                speed=0,
            )


class TestEndToEnd:
    """Broker + publisher + adapter in the same process — the real
    ingestion path with real paho callbacks and real MQTT frames."""

    def test_a1_kickoff_drives_adapter(self, broker):
        captured: list = []

        config = BambuAdapterConfig(
            printer_id="test-a1-live",
            serial="TEST-A1",
            host=broker.host,
            port=broker.port,
            access_code="",           # amqtt anonymous
            use_tls=False,            # embedded broker is plain TCP
        )
        adapter = BambuTelemetryAdapter(config, emitter=captured.append)
        adapter.start()
        try:
            # wait for connection
            assert _wait_for(lambda: any(
                getattr(e, "kind", None) == "connected" for e in captured
            ), timeout=3.0), "adapter did not connect to broker"

            # publish the kickoff fixture
            result = publish_fixture(
                FIXTURES / "bambu-a1-kickoff.jsonl",
                broker.host, broker.port,
                serial="TEST-A1",
                speed=1000.0,
            )
            assert result.messages_published >= 4

            # wait for adapter to observe the final RUNNING state
            assert _wait_for(
                lambda: adapter.status().state == PrinterState.PRINTING,
                timeout=3.0,
            ), f"adapter never reached PRINTING (final: {adapter.status().state})"
        finally:
            adapter.stop()

        # sanity: we received BambuReportEvents through the broker
        report_events = [e for e in captured if isinstance(e, BambuReportEvent)]
        assert len(report_events) >= 3, (
            f"expected 3+ BambuReportEvents, got {len(report_events)}"
        )

        # and at least one state transition happened
        transitions = [e for e in captured if isinstance(e, StateTransitionEvent)]
        assert any(t.to_state == PrinterState.PRINTING for t in transitions)
