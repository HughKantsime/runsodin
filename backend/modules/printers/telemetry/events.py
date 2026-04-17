"""Sealed union of telemetry events that feed the state machine.

Each event carries a `printer_id` + `ts` + a typed payload. The
`transition()` function pattern-matches on the concrete subclass to
decide how to update `PrinterStatus`.

All event classes are frozen dataclasses — the state machine receives
events it doesn't own and must not mutate them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from backend.modules.printers.telemetry.bambu.raw import (
    BambuInfoSection,
    BambuPrintSection,
)


@dataclass(frozen=True)
class BambuReportEvent:
    """A `push_status` message from a Bambu printer (the 99% case)."""

    printer_id: str
    ts: float
    section: BambuPrintSection


@dataclass(frozen=True)
class BambuInfoEvent:
    """A `get_version` response from a Bambu printer — module firmware list."""

    printer_id: str
    ts: float
    section: BambuInfoSection


@dataclass(frozen=True)
class ConnectionEvent:
    """Adapter-level connection state change.

    `kind` values:
    - `connected` — MQTT/WS connection established.
    - `disconnected` — connection lost cleanly.
    - `error` — connection failed or dropped with an error; `detail` has
      the exception repr.
    """

    printer_id: str
    ts: float
    kind: str                     # "connected" | "disconnected" | "error"
    detail: str | None = None


@dataclass(frozen=True)
class HeartbeatMissedEvent:
    """No telemetry received within the OFFLINE_THRESHOLD window.

    Emitted by an external heartbeat watcher, not by the adapter. Fed
    into the state machine to drive the IDLE/PRINTING/PAUSED → OFFLINE
    transition.
    """

    printer_id: str
    ts: float
    last_seen_ts: float


@dataclass(frozen=True)
class DegradedEvent:
    """A telemetry message arrived but could not be modeled.

    Used for the fail-loud path: if `BambuReport.model_validate()`
    raises, the adapter emits this so the UI can reflect the degraded
    state and the observer sees the cause.
    """

    printer_id: str
    ts: float
    reason: str                   # short human message
    raw_excerpt: str | None = None  # first 200 chars of offending payload


# Type alias — the sealed union. New event kinds get added here AND to
# the match statement in `transition()`; the Union makes forgetting one
# half a type error in strict mypy mode.
TelemetryEvent = Union[
    BambuReportEvent,
    BambuInfoEvent,
    ConnectionEvent,
    HeartbeatMissedEvent,
    DegradedEvent,
]
