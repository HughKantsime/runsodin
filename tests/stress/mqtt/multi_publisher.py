"""Spawn N paho.mqtt.Client publishers, each replaying one synthetic stream.

Mirrors production's threading model on the publisher side: one paho client
per printer, `loop_start()` per client, no shared connection. The point isn't
that this is the right shape — it's that it matches what real Bambu printers
inflict on the broker.

Pacing: instead of replaying real ~30s gaps between Bambu pushes, we drive
each printer at a fixed `--rate` (default 10s) so the matrix can be compared
across runs. Each printer gets a phase offset = (printer_index * rate / N) so
heartbeats don't all align in the same second.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion

from .fixture_multiplier import (
    FixtureEvent,
    multiply,
    synthetic_serial,
    synthetic_topic,
)

log = logging.getLogger("stress.publisher")


@dataclass
class PublisherStats:
    publishes: int = 0
    failures: int = 0
    connect_attempts: int = 0
    connect_successes: int = 0
    first_publish_at: Optional[float] = None
    last_publish_at: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_publish(self, ok: bool) -> None:
        now = time.time()
        with self.lock:
            if ok:
                self.publishes += 1
                if self.first_publish_at is None:
                    self.first_publish_at = now
                self.last_publish_at = now
            else:
                self.failures += 1


class SyntheticPrinter:
    """One paho client looping one fixture-derived stream."""

    def __init__(
        self,
        index: int,
        events: list[FixtureEvent],
        broker_host: str,
        broker_port: int,
        rate_s: float,
        phase_offset_s: float,
        stats: PublisherStats,
        stop_event: threading.Event,
    ):
        self.index = index
        self.serial = synthetic_serial(index)
        self.topic = synthetic_topic(index)
        self.events = events
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.rate_s = rate_s
        self.phase_offset_s = phase_offset_s
        self.stats = stats
        self.stop_event = stop_event
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=f"stress-{self.serial}",
            clean_session=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self._thread: Optional[threading.Thread] = None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self.stats.lock:
            self.stats.connect_attempts += 1
            if reason_code == 0 or (hasattr(reason_code, "is_failure") and not reason_code.is_failure):
                self.stats.connect_successes += 1

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        # Paho will auto-reconnect via loop_start; we just log.
        pass

    def start(self) -> None:
        self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()
        self._thread = threading.Thread(target=self._publish_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _publish_loop(self) -> None:
        # Phase offset so 50 printers don't all heartbeat in second 0.
        if self.phase_offset_s > 0:
            self._sleep_or_stop(self.phase_offset_s)
        i = 0
        n = len(self.events)
        while not self.stop_event.is_set():
            evt = self.events[i % n]
            i += 1
            try:
                info = self.client.publish(self.topic, evt.payload, qos=1, retain=False)
                ok = info.rc == mqtt.MQTT_ERR_SUCCESS
                self.stats.record_publish(ok)
            except Exception:
                self.stats.record_publish(False)
            self._sleep_or_stop(self.rate_s)

    def _sleep_or_stop(self, duration_s: float) -> None:
        deadline = time.time() + duration_s
        while time.time() < deadline:
            if self.stop_event.wait(timeout=min(0.5, deadline - time.time())):
                return


def main() -> int:
    parser = argparse.ArgumentParser(description="MQTT stress publisher")
    parser.add_argument("--printers", type=int, required=True, help="N synthetic printers")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/telemetry/bambu-x1c-ams-swap.jsonl"),
        help="Source .jsonl fixture",
    )
    parser.add_argument("--broker-host", default="127.0.0.1")
    parser.add_argument("--broker-port", type=int, default=1883)
    parser.add_argument("--rate", type=float, default=10.0, help="Heartbeat seconds per printer")
    parser.add_argument("--duration", type=float, default=300.0, help="Run for N seconds then exit")
    parser.add_argument(
        "--connect-rate",
        type=float,
        default=100.0,
        help="Cap on new paho client connects per second (spread the cold-start "
        "storm; real fleets come online over hours, not seconds — at N>=400 the "
        "test harness hits OS-level connect_async throttling without this gate).",
    )
    parser.add_argument("--out", type=Path, default=Path("stress-out/publisher.json"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    random.seed(args.seed)

    if not args.fixture.exists():
        log.error("fixture not found: %s", args.fixture)
        return 2

    log.info("loading fixture %s", args.fixture)
    # All printers share the same multiplied-event list shape; we generate
    # per-printer because topics are rewritten per index.
    stats = PublisherStats()
    stop_event = threading.Event()

    printers: list[SyntheticPrinter] = []
    for i in range(args.printers):
        evts = list(multiply(args.fixture, i))
        if not evts:
            log.error("fixture produced zero events for printer %d", i)
            return 2
        phase = (i / max(args.printers, 1)) * args.rate
        printers.append(
            SyntheticPrinter(
                index=i,
                events=evts,
                broker_host=args.broker_host,
                broker_port=args.broker_port,
                rate_s=args.rate,
                phase_offset_s=phase,
                stats=stats,
                stop_event=stop_event,
            )
        )

    log.info(
        "starting %d publishers against %s:%d rate=%.1fs duration=%.1fs connect_rate=%.1f/s",
        args.printers, args.broker_host, args.broker_port, args.rate, args.duration, args.connect_rate,
    )
    connect_interval = 1.0 / max(args.connect_rate, 0.001)
    for p in printers:
        p.start()
        if connect_interval > 0:
            time.sleep(connect_interval)

    def _shutdown(signum, frame):
        log.info("signal %d received, stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    deadline = time.time() + args.duration
    try:
        while time.time() < deadline and not stop_event.is_set():
            time.sleep(1)
    finally:
        log.info("stopping publishers")
        stop_event.set()
        for p in printers:
            p.stop()

    summary = {
        "printers": args.printers,
        "rate_s": args.rate,
        "duration_s": args.duration,
        "broker": f"{args.broker_host}:{args.broker_port}",
        "fixture": str(args.fixture),
        "publishes": stats.publishes,
        "failures": stats.failures,
        "connect_attempts": stats.connect_attempts,
        "connect_successes": stats.connect_successes,
        "first_publish_at": stats.first_publish_at,
        "last_publish_at": stats.last_publish_at,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    log.info("wrote %s — %d publishes, %d failures", args.out, stats.publishes, stats.failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
