"""Demo engine — multi-printer synchronized replay against a live broker.

For marketing footage, sales demos, and agent-demo recordings. Spins up
the local broker (live_replay.LocalBroker), publishes one or more
fixture JSONL streams simultaneously with wall-clock pacing, exposes
runtime controls (pause/resume/seek/speed) via a shared `DemoState`.

Intended usage:
    engine = DemoEngine.from_scenario("dramatic-failure")
    engine.start()          # broker spins up, publishers begin
    engine.pause()
    engine.seek_to(ts=...)
    engine.set_speed(5.0)
    engine.stop()

Or as a CLI (see `cli.py` — not this module):
    python -m backend.modules.printers.telemetry.demo dramatic-failure

Scenario definition (`scenarios/<name>/scenario.yaml`):
    name: dramatic-failure
    description: H2D mid-print failure + recovery
    printers:
      - id: h2d-01
        display_name: "Workshop — H2D"
        serial: "0948AD561201838"
        fixture: bambu-h2d-failure-arc.jsonl
        model: H2D
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from backend.modules.printers.telemetry.live_replay import (
    LocalBroker,
    publish_fixture,
)

logger = logging.getLogger(__name__)

def _find_iso_index(events: list, target_iso: str) -> Optional[int]:
    """Binary search for first event with iso >= target. Events are
    pre-sorted by capture order which equals iso order."""
    if not events:
        return None
    lo, hi = 0, len(events)
    while lo < hi:
        mid = (lo + hi) // 2
        if events[mid][0] < target_iso:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(events) else None


SCENARIOS_DIR = Path(__file__).parent.parent.parent.parent / "demo_scenarios"
# Falls back to committed fixtures dir for fixture lookup.
FIXTURES_DIR = Path(__file__).parent.parent.parent.parent.parent / "tests" / "fixtures" / "telemetry"


@dataclass
class DemoPrinter:
    """One printer in a demo scenario."""

    id: str
    display_name: str
    serial: str
    fixture: str
    model: str = "H2D"

    @property
    def topic_report(self) -> str:
        return f"device/{self.serial}/report"


@dataclass
class DemoScenario:
    """A named collection of printers + fixtures to replay together."""

    name: str
    description: str
    printers: list[DemoPrinter]

    @classmethod
    def load(cls, scenarios_dir: Path, name: str) -> "DemoScenario":
        if yaml is None:
            raise RuntimeError(
                "PyYAML not installed; demo scenarios require `pip install pyyaml`"
            )
        path = scenarios_dir / name / "scenario.yaml"
        if not path.exists():
            raise FileNotFoundError(f"scenario not found: {path}")
        data = yaml.safe_load(path.read_text())
        return cls(
            name=data["name"],
            description=data["description"],
            printers=[DemoPrinter(**p) for p in data["printers"]],
        )


@dataclass
class DemoState:
    """Runtime control state, shared across all publisher threads.

    Threads check `paused` before each publish; `speed` is read at
    each event's pacing delay; `stop_requested` lets them exit cleanly.
    `seek_to_iso` is picked up once per loop iteration and cleared.
    `loop_window` is a (start_iso, end_iso) pair that, when set,
    causes publishers to rewind to `start_iso` whenever they cross
    `end_iso` — repeated until cleared or `stop()`.
    """

    paused: threading.Event = field(default_factory=threading.Event)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    speed: float = 1.0
    seek_to_iso: Optional[str] = None
    loop_window: Optional[tuple[str, str]] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be > 0, got {speed}")
        with self._lock:
            self.speed = speed

    def request_seek(self, iso: str) -> None:
        """Request that publishers jump to the first event at or after `iso`."""
        with self._lock:
            self.seek_to_iso = iso

    def consume_seek(self) -> Optional[str]:
        """Publisher reads + clears the seek request. Thread-safe."""
        with self._lock:
            target = self.seek_to_iso
            self.seek_to_iso = None
            return target

    def set_loop(self, start_iso: str, end_iso: str) -> None:
        if start_iso >= end_iso:
            raise ValueError(f"loop window reversed: {start_iso} >= {end_iso}")
        with self._lock:
            self.loop_window = (start_iso, end_iso)

    def clear_loop(self) -> None:
        with self._lock:
            self.loop_window = None


class DemoEngine:
    """Orchestrates broker + multi-publisher threads for one scenario."""

    def __init__(
        self,
        scenario: DemoScenario,
        fixtures_dir: Path = FIXTURES_DIR,
        speed: float = 1.0,
    ):
        self.scenario = scenario
        self.fixtures_dir = fixtures_dir
        self.state = DemoState(speed=speed)
        self._broker: Optional[LocalBroker] = None
        self._threads: list[threading.Thread] = []

    @classmethod
    def from_scenario(
        cls,
        name: str,
        scenarios_dir: Path = SCENARIOS_DIR,
        fixtures_dir: Path = FIXTURES_DIR,
        speed: float = 1.0,
    ) -> "DemoEngine":
        scenario = DemoScenario.load(scenarios_dir, name)
        return cls(scenario, fixtures_dir=fixtures_dir, speed=speed)

    @property
    def broker_url(self) -> str:
        if self._broker is None:
            raise RuntimeError("engine not started — no broker URL")
        return f"mqtt://{self._broker.host}:{self._broker.port}"

    def start(self) -> None:
        """Spawn broker + one publisher thread per printer."""
        if self._broker is not None:
            raise RuntimeError("engine already started")
        self._broker = LocalBroker()
        self._broker.start()
        for printer in self.scenario.printers:
            fixture_path = self.fixtures_dir / printer.fixture
            if not fixture_path.exists():
                raise FileNotFoundError(
                    f"scenario {self.scenario.name!r} printer {printer.id!r} "
                    f"fixture not found: {fixture_path}"
                )
            t = threading.Thread(
                target=self._publish_loop,
                args=(printer, fixture_path),
                name=f"demo-{printer.id}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        logger.info(
            "demo engine started: scenario=%s printers=%d broker=%s",
            self.scenario.name, len(self.scenario.printers), self.broker_url,
        )

    def _publish_loop(self, printer: DemoPrinter, fixture_path: Path) -> None:
        """One printer's publisher — runs in its own thread.

        Honors the shared DemoState for pause/speed/stop/seek/loop.
        Seek and loop require rewinding the file, so we load the
        fixture into memory up front (tens of MB max — acceptable).
        """
        import json
        import paho.mqtt.client as mqtt

        if self._broker is None:
            return

        # Load + pre-filter in memory for O(1) seek
        events: list[tuple[str, float, dict]] = []
        with fixture_path.open() as f:
            for raw in f:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if parsed.get("direction") != "recv":
                    continue
                payload = parsed.get("payload")
                if not isinstance(payload, dict):
                    continue
                iso = parsed.get("iso") or ""
                ts = float(parsed.get("ts", 0))
                events.append((iso, ts, payload))

        topic = printer.topic_report
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        client.connect(self._broker.host, self._broker.port, keepalive=30)
        client.loop_start()
        try:
            cursor = 0
            prev_ts: Optional[float] = None
            while cursor < len(events):
                if self.state.stop_requested.is_set():
                    break

                # pause gate
                while self.state.paused.is_set():
                    if self.state.stop_requested.is_set():
                        break
                    time.sleep(0.05)
                if self.state.stop_requested.is_set():
                    break

                # seek request: jump cursor to first event at/after target iso
                seek_target = self.state.consume_seek()
                if seek_target is not None:
                    new_cursor = _find_iso_index(events, seek_target)
                    if new_cursor is not None:
                        cursor = new_cursor
                        prev_ts = None  # reset pacing after seek

                iso, ts, payload = events[cursor]

                # loop window: if we've crossed end_iso, rewind to start_iso
                with self.state._lock:
                    window = self.state.loop_window
                if window is not None and iso >= window[1]:
                    new_cursor = _find_iso_index(events, window[0])
                    if new_cursor is not None:
                        cursor = new_cursor
                        prev_ts = None
                        continue  # re-read event at new cursor

                # pacing based on ts delta
                if prev_ts is not None:
                    gap = ts - prev_ts
                    if gap > 300.0:
                        gap = 300.0
                    wait = gap / max(self.state.speed, 0.001)
                    if wait > 0:
                        end = time.time() + wait
                        while time.time() < end:
                            if self.state.stop_requested.is_set():
                                break
                            if self.state.consume_seek() is not None:
                                # a seek during a sleep — break out; next loop
                                # iteration will handle it (we consumed it, but
                                # also need to not advance cursor)
                                break
                            remaining = end - time.time()
                            if remaining <= 0:
                                break
                            time.sleep(min(0.05, remaining))
                prev_ts = ts

                client.publish(topic, json.dumps(payload), qos=0)
                cursor += 1
        finally:
            client.loop_stop()
            client.disconnect()

    def seek_to(self, iso: str) -> None:
        """Jump all publishers to the first event at or after `iso`."""
        self.state.request_seek(iso)

    def set_loop(self, start_iso: str, end_iso: str) -> None:
        """Publishers rewind to `start_iso` whenever they cross `end_iso`."""
        self.state.set_loop(start_iso, end_iso)

    def clear_loop(self) -> None:
        self.state.clear_loop()

    def pause(self) -> None:
        self.state.paused.set()

    def resume(self) -> None:
        self.state.paused.clear()

    def set_speed(self, speed: float) -> None:
        self.state.set_speed(speed)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop all publishers, shut down broker."""
        self.state.stop_requested.set()
        # Wake paused threads
        self.state.paused.clear()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []
        if self._broker is not None:
            self._broker.stop()
            self._broker = None

    def wait_until_done(self, timeout: Optional[float] = None) -> bool:
        """Block until all publisher threads finish their fixture.

        Returns True if all finished, False on timeout.
        """
        deadline = time.time() + timeout if timeout is not None else None
        for t in self._threads:
            remaining = None if deadline is None else max(0, deadline - time.time())
            t.join(timeout=remaining)
            if t.is_alive():
                return False
        return True
