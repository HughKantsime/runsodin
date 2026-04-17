"""Live-MQTT replay for demo footage and end-to-end adapter testing.

Spawns a real mosquitto broker subprocess, lets a publisher replay
fixture JSONL to `device/<serial>/report`, and (optionally) connects a
real `BambuTelemetryAdapter` to receive. The adapter is unchanged —
same production code connecting to a broker on 127.0.0.1.

This closes the loop that was split in Phase 3: the in-process
`replay()` path (feeds events to `transition()` directly) proves the
state-machine behavior in CI. This module proves the **adapter**
end-to-end — MQTT subscribe, paho callbacks, V2 pipeline — using real
broker traffic.

Broker choice: native mosquitto subprocess. Tried amqtt first; its
anonymous-auth plugin accepted TCP but wouldn't CONNACK for paho
clients. Mosquitto handshakes cleanly with paho MQTT 3.1.1 and is
already available on the M4 CI runner (`brew install mosquitto`).

Used by:
- Phase 6 demo scenarios (`replayer demo <scenario>`).
- Integration tests that cover the full MQTT-to-state-machine path.
- Future live shadow-mode adapter.
"""
from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

from backend.modules.printers.telemetry.replay import MAX_GAP_SEC

logger = logging.getLogger(__name__)


# ===== Broker =====

def _free_port() -> int:
    """Grab an available localhost TCP port. Window is small — caller
    should start the broker immediately after this returns."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LocalBroker:
    """Mosquitto subprocess bound to a random 127.0.0.1 port.

    Lifecycle:
        broker = LocalBroker()
        broker.start()       # subprocess + readiness probe
        ...                  # adapter/publisher connect to broker.host:port
        broker.stop()

    Requires `mosquitto` on PATH. Detection + helpful error on missing.
    """

    MOSQUITTO_STARTUP_TIMEOUT = 3.0

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port = _free_port()
        self._proc: Optional[subprocess.Popen] = None
        self._config_dir: Optional[tempfile.TemporaryDirectory] = None

    @staticmethod
    def _mosquitto_binary() -> str:
        path = shutil.which("mosquitto")
        if path is None:
            raise RuntimeError(
                "mosquitto binary not on PATH. Install via "
                "`brew install mosquitto` or equivalent before running "
                "live-broker tests."
            )
        return path

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("broker already started")
        binary = self._mosquitto_binary()
        self._config_dir = tempfile.TemporaryDirectory(prefix="odin-mqtt-")
        cfg_path = Path(self._config_dir.name) / "mosquitto.conf"
        cfg_path.write_text(
            f"listener {self.port} {self.host}\n"
            "allow_anonymous true\n"
            "persistence false\n"
            # Keep logs on stderr, quiet.
            "log_dest stderr\n"
            "log_type error\n"
        )
        self._proc = subprocess.Popen(
            [binary, "-c", str(cfg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Probe the port until it accepts connections or we time out.
        deadline = time.time() + self.MOSQUITTO_STARTUP_TIMEOUT
        while time.time() < deadline:
            s = socket.socket()
            s.settimeout(0.2)
            try:
                s.connect((self.host, self.port))
                s.close()
                return
            except (ConnectionRefusedError, socket.timeout):
                time.sleep(0.05)
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        self.stop()
        raise RuntimeError(
            f"mosquitto did not accept connections on "
            f"{self.host}:{self.port} within "
            f"{self.MOSQUITTO_STARTUP_TIMEOUT}s"
        )

    def stop(self, timeout: float = 2.0) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=1.0)
            self._proc = None
        if self._config_dir is not None:
            self._config_dir.cleanup()
            self._config_dir = None


# ===== Publisher =====

@dataclass
class PublishResult:
    """Summary returned by `publish_fixture`."""

    messages_published: int
    lines_skipped: int          # capture lines that weren't MQTT recv events
    duration_sec: float


def publish_fixture(
    fixture_path: Path,
    broker_host: str,
    broker_port: int,
    serial: str,
    topic_prefix: str = "device",
    speed: float = 100.0,
) -> PublishResult:
    """Replay a JSONL fixture to `<topic_prefix>/<serial>/report`.

    Blocks until done. Respects ISO timestamps for pacing — `speed=1`
    replays at wall-clock, `speed=100` replays 100× faster. Gaps above
    `MAX_GAP_SEC` compress to 0.01s/speed (demos shouldn't stall on
    idle windows).

    Suitable for demo mode (speed=1 to 10) and for end-to-end integration
    tests (speed=1000+).
    """
    if speed <= 0:
        raise ValueError(f"speed must be > 0, got {speed}")

    topic = f"{topic_prefix}/{serial}/report"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
    client.connect(broker_host, broker_port, keepalive=30)
    client.loop_start()

    published = 0
    skipped = 0
    start_wall = time.time()
    prev_ts: Optional[float] = None
    try:
        with fixture_path.open() as f:
            for raw_line in f:
                try:
                    parsed = json.loads(raw_line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if parsed.get("direction") != "recv":
                    skipped += 1
                    continue
                payload = parsed.get("payload")
                if not isinstance(payload, dict):
                    skipped += 1
                    continue

                # pacing based on ts delta
                ts = float(parsed.get("ts", 0))
                if prev_ts is not None:
                    gap = ts - prev_ts
                    if gap > MAX_GAP_SEC:
                        gap = MAX_GAP_SEC
                    wait = gap / speed
                    if wait > 0:
                        time.sleep(wait)
                prev_ts = ts

                client.publish(topic, json.dumps(payload), qos=0)
                published += 1
    finally:
        client.loop_stop()
        client.disconnect()

    return PublishResult(
        messages_published=published,
        lines_skipped=skipped,
        duration_sec=time.time() - start_wall,
    )
