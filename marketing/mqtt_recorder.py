#!/usr/bin/env python3
"""MQTT Recorder — subscribes to all topics on a broker and records every
message with timestamps to a JSON file.

Usage example::

    python mqtt_recorder.py --broker 10.0.0.50 --output recordings/session.json

Press Ctrl+C to stop recording early.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_payload(raw: bytes):
    """Try to decode a payload as JSON, then UTF-8 string, else hex."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.hex()


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class MqttRecorder:
    """Records every MQTT message to an in-memory list and writes JSON on
    shutdown."""

    def __init__(self, broker: str, port: int, username: str | None,
                 password: str | None, use_tls: bool, duration: float,
                 output: Path):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.duration = duration
        self.output = output

        self.messages: list[dict] = []
        self._start_mono: float = 0.0
        self._start_wall: float = 0.0
        self._running = True

    # -- MQTT callbacks (paho v2 signature) --------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("Connected to %s:%s — subscribing to '#'", self.broker,
                     self.port)
            client.subscribe("#")
        else:
            log.error("Connection failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        elapsed = round(time.monotonic() - self._start_mono, 3)
        record = {
            "t": elapsed,
            "topic": msg.topic,
            "payload": _decode_payload(msg.payload),
            "qos": msg.qos,
        }
        self.messages.append(record)
        count = len(self.messages)
        if count % 100 == 0:
            log.info("Recorded %d messages (%.1f s elapsed)", count, elapsed)

    # -- Main loop ---------------------------------------------------------

    def run(self):
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = self._on_connect
        client.on_message = self._on_message

        if self.username:
            client.username_pw_set(self.username, self.password)

        if self.use_tls:
            import ssl
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)

        # Graceful shutdown on SIGINT
        def _handle_sigint(signum, frame):
            log.info("SIGINT received — stopping recording")
            self._running = False

        signal.signal(signal.SIGINT, _handle_sigint)

        log.info("Connecting to %s:%s (TLS=%s) …", self.broker, self.port,
                 self.use_tls)
        client.connect(self.broker, self.port)
        client.loop_start()

        self._start_mono = time.monotonic()
        self._start_wall = time.time()

        try:
            deadline = self._start_mono + self.duration
            while self._running and time.monotonic() < deadline:
                time.sleep(0.25)
        finally:
            client.loop_stop()
            client.disconnect()

        actual_duration = round(time.monotonic() - self._start_mono, 3)
        self._write_output(actual_duration)

    def _write_output(self, actual_duration: float):
        result = {
            "broker": self.broker,
            "message_count": len(self.messages),
            "duration_seconds": actual_duration,
            "messages": self.messages,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(result, indent=2))
        log.info("Wrote %d messages (%.1f s) to %s", len(self.messages),
                 actual_duration, self.output)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record all MQTT messages from a broker to a JSON file.",
    )
    parser.add_argument("--broker", required=True,
                        help="MQTT broker hostname or IP")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MQTT_BROKER_PORT", "8883")),
                        help="Broker port (default: env MQTT_BROKER_PORT or 8883)")
    parser.add_argument("--username",
                        default=os.environ.get("MQTT_USERNAME"),
                        help="MQTT username (default: env MQTT_USERNAME)")
    parser.add_argument("--password",
                        default=os.environ.get("MQTT_PASSWORD"),
                        help="MQTT password (default: env MQTT_PASSWORD)")
    parser.add_argument("--tls", dest="tls", action="store_true", default=True,
                        help="Enable TLS (default)")
    parser.add_argument("--no-tls", dest="tls", action="store_false",
                        help="Disable TLS")
    parser.add_argument("--duration", type=float, default=1800,
                        help="Recording duration in seconds (default: 1800)")
    parser.add_argument("--output", required=True, type=Path,
                        help="Output JSON file path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    recorder = MqttRecorder(
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        use_tls=args.tls,
        duration=args.duration,
        output=args.output,
    )
    recorder.run()


if __name__ == "__main__":
    main()
