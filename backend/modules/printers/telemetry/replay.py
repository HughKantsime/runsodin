"""In-process replay of captured telemetry into the state machine.

This module is the **testing** replayer: it reads a JSONL capture,
converts each line into a TelemetryEvent, and feeds events to
`transition()` in ISO-timestamp order. No MQTT. No WebSocket. No wall-
clock pacing (tests run as fast as the CPU allows).

The **demo** replayer — which speaks real MQTT to a local broker and
paces at wall-clock for marketing footage — is a separate concern
that lives outside this repo (deferred to a follow-up track).

Capture-line → event mapping:
- `{"payload": {"print": {...}}}` → BambuReportEvent
- `{"payload": {"info": {...}}}` → BambuInfoEvent
- `{"direction": "error", ...}` → ConnectionEvent(kind="error")
- `{"event": "subscribed", ...}` → ConnectionEvent(kind="connected")
- Anything we can't classify → DegradedEvent

Gap compression: if two consecutive events have a ts gap > MAX_GAP_SEC
(default 300 s = 5 min), the gap is compressed by treating the second
event as if it happened MAX_GAP_SEC after the first. This matters
because real captures have long idle windows (~2 h between prints).
Without compression, a full-capture replay would appear frozen.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from backend.modules.printers.telemetry.bambu.raw import (
    BambuInfoSection,
    BambuPrintSection,
    BambuReport,
    InvalidBambuReport,
)
from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    TelemetryEvent,
)
from backend.modules.printers.telemetry.observability import observer
from backend.modules.printers.telemetry.state import (
    PrinterStatus,
    StateTransitionEvent,
)
from backend.modules.printers.telemetry.transition import transition


MAX_GAP_SEC = 300.0                 # 5 min — larger gaps compress to this
MAX_VALIDATION_EXCERPT = 200        # chars of payload kept in DegradedEvent


@dataclass(frozen=True)
class ReplayResult:
    """Return value of `replay()` — the final status and every transition."""

    final_status: PrinterStatus
    transitions: list[StateTransitionEvent]
    event_count: int                # total TelemetryEvents generated
    degraded_count: int             # events that couldn't be modeled
    skipped_count: int              # capture lines we deliberately ignored


# ===== Line → event conversion =====

def line_to_event(line: dict, printer_id: str) -> TelemetryEvent | None:
    """Convert one parsed JSONL line into a TelemetryEvent, or return
    None if the line is deliberately uninteresting (e.g. heartbeat).

    Fail-loud path: if the line looks like a Bambu report but fails
    to validate, returns a DegradedEvent so the replay continues but
    the caller can count how many were degraded.
    """
    ts = float(line.get("ts", 0))
    direction = line.get("direction")

    # Errors from the capture itself (MQTT disconnects etc.)
    if direction == "error":
        return ConnectionEvent(
            printer_id=printer_id,
            ts=ts,
            kind="error",
            detail=str(line.get("error", ""))[:200],
        )

    # Subscribe events in the capture envelope
    if line.get("event") == "subscribed":
        return ConnectionEvent(
            printer_id=printer_id,
            ts=ts,
            kind="connected",
        )

    # The 99% case: payload from MQTT
    payload = line.get("payload")
    if isinstance(payload, dict):
        # try BambuReport first
        try:
            report = BambuReport.model_validate(payload)
        except InvalidBambuReport:
            # payload had neither print nor info — skip (heartbeat-ish)
            return None
        except Exception as exc:
            return DegradedEvent(
                printer_id=printer_id,
                ts=ts,
                reason=str(exc)[:200],
                raw_excerpt=json.dumps(payload)[:MAX_VALIDATION_EXCERPT],
            )

        # surface unmapped fields so the observer snapshot reflects replay
        # alongside live traffic — the allowlist regression test uses this
        if report.model_extra:
            observer.observe("bambu", report.model_extra)
        if report.print is not None and report.print.model_extra:
            observer.observe("bambu", report.print.model_extra)

        if report.print is not None:
            return BambuReportEvent(
                printer_id=printer_id,
                ts=ts,
                section=report.print,
            )
        if report.info is not None:
            return BambuInfoEvent(
                printer_id=printer_id,
                ts=ts,
                section=report.info,
            )
        # report.system: command-ack envelopes — not telemetry; skip
        return None

    # Unclassified — skip silently (no value in surfacing capture-envelope noise)
    return None


# ===== Event stream from a JSONL path =====

def iter_events(path: Path, printer_id: str | None = None) -> Iterator[TelemetryEvent]:
    """Stream TelemetryEvents out of a JSONL capture file.

    Yields events in file order (which IS ts order — captures are
    written append-only). Skips lines that aren't meaningful events.
    Applies gap compression so the caller doesn't see huge ts jumps.

    `printer_id`: override the capture's own printer_id (useful when
    replaying for contract tests where the id is a test-local string).
    If None, uses the `printer_id` from each line.
    """
    prev_event_ts: float | None = None
    ts_offset: float = 0.0             # accumulated compression offset

    with path.open() as f:
        for line_num, raw in enumerate(f, start=1):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            pid = printer_id or parsed.get("printer_id") or "unknown"
            event = line_to_event(parsed, pid)
            if event is None:
                continue

            # gap compression
            real_ts = event.ts
            if prev_event_ts is not None:
                gap = real_ts - prev_event_ts
                if gap > MAX_GAP_SEC:
                    ts_offset += gap - MAX_GAP_SEC
            prev_event_ts = real_ts
            adjusted_ts = real_ts - ts_offset

            yield _with_ts(event, adjusted_ts)


def _with_ts(event: TelemetryEvent, ts: float) -> TelemetryEvent:
    """Return `event` with a new ts. Frozen dataclasses need `replace()`."""
    from dataclasses import replace
    return replace(event, ts=ts)


# ===== Replay a full capture or slice through the state machine =====

def replay(
    path: Path,
    printer_id: str | None = None,
    initial_status: PrinterStatus | None = None,
) -> ReplayResult:
    """Drive events from `path` through `transition()` to completion.

    Returns the final status and the sequence of emitted transitions —
    the unit of comparison for snapshot tests.
    """
    status = initial_status or PrinterStatus.initial()
    transitions: list[StateTransitionEvent] = []
    event_count = 0
    degraded_count = 0
    skipped_count = 0

    for event in iter_events(path, printer_id=printer_id):
        event_count += 1
        if isinstance(event, DegradedEvent):
            degraded_count += 1
        try:
            status, new_transitions = transition(status, event)
        except Exception:
            # Transition-level fail-loud bubbles up — test expected.
            raise
        transitions.extend(new_transitions)

    return ReplayResult(
        final_status=status,
        transitions=transitions,
        event_count=event_count,
        degraded_count=degraded_count,
        skipped_count=skipped_count,
    )


# ===== Slicer =====

def slice_capture(
    input_path: Path,
    output_path: Path,
    start_iso: str,
    end_iso: str,
) -> int:
    """Write a JSONL slice of `input_path` covering `[start_iso, end_iso]`.

    Returns the number of lines written. Fails loud if the date range
    is reversed or the input file is missing.
    """
    if start_iso >= end_iso:
        raise ValueError(f"reversed or empty range: {start_iso!r} >= {end_iso!r}")
    if not input_path.exists():
        raise FileNotFoundError(f"input capture not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with input_path.open() as fin, output_path.open("w") as fout:
        for line in fin:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            iso = parsed.get("iso")
            if iso is None:
                continue
            if start_iso <= iso <= end_iso:
                fout.write(line if line.endswith("\n") else line + "\n")
                written += 1
    return written


# ===== Expected-states bootstrap =====

def bootstrap_expected_states(
    fixture_path: Path,
    printer_id: str | None = None,
) -> dict:
    """Run `replay()` and serialize the result as an `expected.json`
    snapshot for contract tests.

    Output shape:
    {
        "fixture": "<filename>",
        "printer_id": "...",
        "event_count": N,
        "degraded_count": N,
        "final_state": "printing" | ...,
        "transitions": [
            {"ts": ..., "from_state": "idle", "to_state": "preparing", "reason": "..."},
            ...
        ]
    }

    Human-reviewed + committed alongside the fixture. Subsequent
    contract test runs compare against this snapshot.
    """
    result = replay(fixture_path, printer_id=printer_id)
    return {
        "fixture": fixture_path.name,
        "printer_id": printer_id,
        "event_count": result.event_count,
        "degraded_count": result.degraded_count,
        "final_state": result.final_status.state.value,
        "transitions": [
            {
                "ts": t.ts,
                "from_state": t.from_state.value,
                "to_state": t.to_state.value,
                "reason": t.reason,
            }
            for t in result.transitions
        ],
    }
