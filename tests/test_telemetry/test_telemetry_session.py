"""Tests for the Bambu session helpers (read_status_once + run_command).

Uses the live mosquitto broker + a publisher to simulate a real Bambu
responding to a short-lived connect.
"""
from __future__ import annotations

import threading
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from backend.modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
from backend.modules.printers.telemetry.bambu.session import (
    read_status_once,
    run_command,
)
from backend.modules.printers.telemetry.live_replay import (
    LocalBroker,
    publish_fixture,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"


@pytest.fixture
def broker():
    b = LocalBroker()
    b.start()
    yield b
    b.stop()


def _config(broker, serial="TEST-X1C") -> BambuAdapterConfig:
    return BambuAdapterConfig(
        printer_id="test-one-shot",
        serial=serial,
        host=broker.host,
        port=broker.port,
        access_code="",
        use_tls=False,
    )


class TestReadStatusOnce:
    def test_returns_failure_on_unreachable(self):
        """Wrong port → CONNACK timeout → StatusReadResult(success=False)."""
        config = BambuAdapterConfig(
            printer_id="x", serial="S", host="127.0.0.1",
            port=9,  # reserved discard port
            access_code="", use_tls=False,
        )
        result = read_status_once(config, timeout=1.0)
        assert result.success is False

    def test_returns_view_after_fixture_pump(self, broker):
        """Spawn adapter, pump a fixture to it via the broker, assert
        session helper gets a usable view."""
        config = _config(broker, serial="TEST-A1")

        # Pump fixture in a background thread so read_status_once can
        # receive messages.
        pump_thread = threading.Thread(
            target=publish_fixture,
            args=(FIXTURES / "bambu-a1-kickoff.jsonl", broker.host, broker.port),
            kwargs={"serial": "TEST-A1", "speed": 50.0},
            daemon=True,
        )
        pump_thread.start()

        result = read_status_once(config, timeout=5.0)
        pump_thread.join(timeout=5.0)

        assert result.success is True
        assert result.view is not None
        # status should reflect the fixture
        assert result.section is not None


class TestRunCommand:
    def test_pause_print_publishes_to_request_topic(self, broker):
        """Subscribe a listener to the request topic, run pause_print via
        session helper, verify the payload lands."""
        received: list[bytes] = []
        done = threading.Event()

        def on_message(client, userdata, msg):
            received.append(msg.payload)
            done.set()

        listener = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
            client_id="listener-pause",
        )
        listener.on_message = on_message
        listener.connect(broker.host, broker.port, keepalive=30)
        listener.subscribe("device/TEST-CMD/request")
        listener.loop_start()

        try:
            config = _config(broker, serial="TEST-CMD")
            result = run_command(config, "pause_print")
            assert result is True

            assert done.wait(timeout=3.0), "listener never received request"
            assert len(received) == 1
            import json
            payload = json.loads(received[0])
            assert payload == {
                "print": {"sequence_id": "0", "command": "pause"}
            }
        finally:
            listener.loop_stop()
            listener.disconnect()

    def test_unknown_method_returns_false(self, broker):
        config = _config(broker, serial="TEST-X")
        assert run_command(config, "send_a_rocket") is False

    def test_unreachable_returns_false(self):
        config = BambuAdapterConfig(
            printer_id="x", serial="S", host="127.0.0.1",
            port=9, access_code="", use_tls=False,
        )
        assert run_command(config, "pause_print", connect_timeout=0.5) is False
