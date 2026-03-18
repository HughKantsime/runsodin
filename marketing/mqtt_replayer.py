#!/usr/bin/env python3
"""MQTT Replayer — reads a recording JSON file and replays messages to a
target MQTT broker with original timing.

Recording format (produced by mqtt_recorder.py):
    {"messages": [{"t": float, "topic": str, "payload": str|dict, "qos": int}, ...]}

The ``t`` field stores the elapsed seconds since the first captured message.
"""

import argparse
import json
import logging
import os
import signal
import ssl
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

log = logging.getLogger("mqtt_replayer")


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def _on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        log.info("Connected to broker")
        userdata["connected"].set()
    else:
        log.error("Connection failed: %s", reason_code)


def _build_client(args) -> mqtt.Client:
    """Create, configure, and connect an MQTT v5 client."""
    import threading

    userdata = {"connected": threading.Event()}
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        userdata=userdata,
    )
    client.on_connect = _on_connect

    if args.tls:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)

    if args.username:
        client.username_pw_set(args.username, args.password)

    log.info("Connecting to %s:%d (tls=%s) ...", args.broker, args.port, args.tls)
    client.connect(args.broker, args.port)
    client.loop_start()

    if not userdata["connected"].wait(timeout=10):
        log.error("Timed out waiting for broker connection")
        client.loop_stop()
        sys.exit(1)

    return client


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

_stop = False


def _handle_signal(_signum, _frame):
    global _stop
    log.info("Interrupt received — stopping replay")
    _stop = True


def _load_recording(path: Path) -> list[dict]:
    """Load and return the messages list from a recording JSON file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    messages = data.get("messages", [])
    log.info("Loaded %d messages from %s", len(messages), path)
    return messages


def _replay(client: mqtt.Client, messages: list[dict], speed: float) -> None:
    """Play back *messages* once, honouring the original inter-message timing
    scaled by *speed*.
    """
    if not messages:
        log.warning("Recording contains no messages — nothing to replay")
        return

    log.info("Starting replay (%d messages, speed=%.2fx)", len(messages), speed)
    start_wall = time.monotonic()

    for idx, msg in enumerate(messages):
        if _stop:
            log.info("Replay interrupted at message %d/%d", idx, len(messages))
            return

        target_elapsed = msg["t"] / speed
        now_elapsed = time.monotonic() - start_wall
        wait = target_elapsed - now_elapsed
        if wait > 0:
            # Sleep in small increments so we can react to SIGINT promptly.
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not _stop:
                time.sleep(min(0.1, deadline - time.monotonic()))

        if _stop:
            log.info("Replay interrupted at message %d/%d", idx, len(messages))
            return

        payload = msg["payload"]
        if isinstance(payload, dict):
            payload = json.dumps(payload)

        qos = msg.get("qos", 0)
        client.publish(msg["topic"], payload, qos=qos)

        if (idx + 1) % 50 == 0 or idx + 1 == len(messages):
            log.info("Published %d/%d messages", idx + 1, len(messages))

    log.info("Replay complete")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay recorded MQTT messages to a target broker.",
    )
    parser.add_argument(
        "--broker", required=True, help="Target MQTT broker hostname/IP"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MQTT_BROKER_PORT", "8883")),
        help="Broker port (default: env MQTT_BROKER_PORT or 8883)",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("MQTT_USERNAME"),
        help="MQTT username (default: env MQTT_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("MQTT_PASSWORD"),
        help="MQTT password (default: env MQTT_PASSWORD)",
    )
    parser.add_argument(
        "--tls",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable TLS (default: True). Use --no-tls to disable.",
    )
    parser.add_argument(
        "--recording",
        required=True,
        type=Path,
        help="Path to recording JSON file",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback speed multiplier (default: 1.0)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=False,
        help="Loop the recording continuously",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    args = _parse_args(argv)
    messages = _load_recording(args.recording)

    signal.signal(signal.SIGINT, _handle_signal)

    client = _build_client(args)

    try:
        iteration = 0
        while True:
            iteration += 1
            if args.loop:
                log.info("--- Loop iteration %d ---", iteration)
            _replay(client, messages, args.speed)
            if _stop or not args.loop:
                break
    finally:
        client.loop_stop()
        client.disconnect()
        log.info("Disconnected from broker")


if __name__ == "__main__":
    main()
