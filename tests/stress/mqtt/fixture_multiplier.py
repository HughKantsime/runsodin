"""Multiply one captured Bambu MQTT fixture into N synthetic printer streams.

Bambu serial pattern is 15 chars (e.g. `00M09D4B1600284`). We hold the first 11
chars constant and rewrite the last 4 to `{NN:04d}` so each synthetic printer
gets a unique-but-plausible serial. Topic gets rewritten the same way.

Reads a .jsonl file from `tests/fixtures/telemetry/`; each line is a captured
event with `topic` and `payload` keys (legacy capture format).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SERIAL_RE = re.compile(r"device/([^/]+)/report")
SERIAL_PREFIX = "00M00A45000"  # 11 chars; we add 4 numeric to make 15


@dataclass
class FixtureEvent:
    """One MQTT message ready to publish: topic + JSON-encoded payload."""

    topic: str
    payload: bytes
    relative_ts: float  # seconds from fixture start


def synthetic_serial(index: int) -> str:
    """Generate a 15-char Bambu-shaped serial for printer N."""
    if index < 0 or index > 9999:
        raise ValueError(f"index out of range 0-9999: {index}")
    return f"{SERIAL_PREFIX}{index:04d}"


def synthetic_topic(index: int) -> str:
    return f"device/{synthetic_serial(index)}/report"


def load_fixture(path: Path) -> list[dict]:
    """Load + parse one fixture file. Returns list of raw event dicts."""
    events: list[dict] = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    if not events:
        raise ValueError(f"fixture is empty: {path}")
    return events


def multiply(
    fixture_path: Path,
    printer_index: int,
) -> Iterator[FixtureEvent]:
    """Yield FixtureEvents for one synthetic printer derived from one fixture.

    The original `topic` is rewritten to point at the synthetic serial. The
    `ts` of the first event is the zero point; each subsequent event carries
    its delta. Caller decides how to space them in wall time.
    """
    raw = load_fixture(fixture_path)
    new_topic = synthetic_topic(printer_index)
    base_ts = raw[0]["ts"]
    for evt in raw:
        if "topic" in evt and not SERIAL_RE.match(evt["topic"]):
            # not a device/<serial>/report event — skip
            continue
        payload = evt.get("payload")
        if payload is None:
            continue
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        yield FixtureEvent(
            topic=new_topic,
            payload=encoded,
            relative_ts=evt["ts"] - base_ts,
        )


def fixture_summary(fixture_path: Path) -> dict:
    """Quick stats on a fixture for logging/CLI."""
    raw = load_fixture(fixture_path)
    return {
        "path": str(fixture_path),
        "events": len(raw),
        "duration_s": raw[-1]["ts"] - raw[0]["ts"] if len(raw) > 1 else 0.0,
        "first_topic": raw[0].get("topic"),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: fixture_multiplier.py <fixture.jsonl> [printer_index]")
        sys.exit(2)
    p = Path(sys.argv[1])
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    print(json.dumps(fixture_summary(p), indent=2))
    print(f"\nsynthetic serial: {synthetic_serial(idx)}")
    print(f"synthetic topic:  {synthetic_topic(idx)}")
    count = sum(1 for _ in multiply(p, idx))
    print(f"multiplied events: {count}")
