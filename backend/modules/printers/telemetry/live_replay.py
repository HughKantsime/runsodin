"""Live-MQTT replay for demo footage and end-to-end adapter testing.

Spawns an in-process amqtt broker, lets a publisher replay fixture
JSONL to `device/<serial>/report`, and (optionally) connects a real
`BambuTelemetryAdapter` to receive. The adapter is unchanged — it's
the same production code connecting to a broker on 127.0.0.1.

This closes the loop that was split in Phase 3: the in-process
`replay()` path (which feeds events to `transition()` directly) proves
the state-machine behavior in CI. This module proves the **adapter**
end-to-end — MQTT subscribe, paho callbacks, V2 pipeline — using real
broker traffic.

Pure-Python stack (no Docker, no native mosquitto dep):
- `amqtt` broker embedded on a random high port.
- `paho.mqtt.client` as publisher (matches production adapter's
  client lib; consistency is a feature).
- Adapter side uses whatever the caller injects (normally
  `BambuTelemetryAdapter`).

Used by:
- Phase 6 demo scenarios (`replayer demo <scenario>`).
- Integration tests that cover the full MQTT-to-state-machine path.
- The follow-up track's live shadow-mode adapter (legacy + V2 running
  against the same broker from fixture traffic).
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
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
    """Embedded amqtt broker on a random 127.0.0.1 port.

    Lifecycle:
        broker = LocalBroker()
        broker.start()       # background thread, ready ~100ms
        ...                  # adapter/publisher connect to broker.host:port
        broker.stop()

    Thread-safe `start()` / `stop()`; `host`/`port` valid after `start()`.
    """

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port = _free_port()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._broker = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()

    def start(self) -> None:
        """Start broker in a background thread. Blocks until ready."""
        if self._thread is not None:
            raise RuntimeError("broker already started")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("broker failed to start within 5s")

    def _run(self) -> None:
        from amqtt.broker import Broker
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{self.host}:{self.port}",
                    "max_connections": 50,
                },
            },
            "sys_interval": 0,
            "auth": {"allow-anonymous": True, "plugins": []},
            "topic-check": {"enabled": False},
        }

        async def _lifecycle():
            # Broker() must be constructed INSIDE the running loop —
            # asyncio.get_running_loop() is called in __init__.
            self._broker = Broker(config)
            await self._broker.start()
            self._ready.set()
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.05)
            await self._broker.shutdown()

        try:
            self._loop.run_until_complete(_lifecycle())
        finally:
            self._loop.close()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the background thread to shut down and join."""
        if self._thread is None:
            return
        self._stop_requested.set()
        self._thread.join(timeout=timeout)
        self._thread = None


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
