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
    """

    paused: threading.Event = field(default_factory=threading.Event)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    speed: float = 1.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError(f"speed must be > 0, got {speed}")
        with self._lock:
            self.speed = speed


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

        Honors the shared DemoState for pause + speed + stop. Uses
        `publish_fixture` under the hood but with a periodic pause-check
        wrapper (publish_fixture itself doesn't know about DemoState).
        """
        # We re-implement the pacing loop inline here because we need
        # to poll state.paused and state.stop_requested between events.
        import json
        import paho.mqtt.client as mqtt

        if self._broker is None:
            return

        topic = printer.topic_report
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        client.connect(self._broker.host, self._broker.port, keepalive=30)
        client.loop_start()
        try:
            prev_ts: Optional[float] = None
            with fixture_path.open() as f:
                for raw in f:
                    if self.state.stop_requested.is_set():
                        break
                    while self.state.paused.is_set():
                        if self.state.stop_requested.is_set():
                            break
                        time.sleep(0.05)
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if parsed.get("direction") != "recv":
                        continue
                    payload = parsed.get("payload")
                    if not isinstance(payload, dict):
                        continue

                    ts = float(parsed.get("ts", 0))
                    if prev_ts is not None:
                        gap = ts - prev_ts
                        if gap > 300.0:
                            gap = 300.0
                        wait = gap / max(self.state.speed, 0.001)
                        if wait > 0:
                            # sleep in small ticks so pause/stop respond quickly
                            end = time.time() + wait
                            while time.time() < end:
                                if self.state.stop_requested.is_set():
                                    break
                                remaining = end - time.time()
                                if remaining <= 0:
                                    break
                                time.sleep(min(0.05, remaining))
                    prev_ts = ts
                    client.publish(topic, json.dumps(payload), qos=0)
        finally:
            client.loop_stop()
            client.disconnect()

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
