"""Canonical printer state machine — vendor-agnostic.

The V2 pipeline's core: a **pure function** `transition(prev, event)`
that returns a new `PrinterStatus` plus a list of transitions to emit.
No I/O, no wall-clock, no random. Given the same input, produces the
same output every time. This is what makes the replayer deterministic
and makes contract tests possible.

Key distinction from the legacy adapter:

- `PrinterState` has **separate** members for `FAILED`, `FINISHED`,
  `ERROR`, `DEGRADED`, and `IDLE`. The legacy adapter collapsed
  `FAILED` and `FINISH` both to `IDLE` — making real failures invisible
  in the UI the moment they happened. V2 surfaces them distinctly.

- `ERROR` (printer hardware/firmware error) is distinct from `FAILED`
  (a print job ended abnormally). A printer in `ERROR` state cannot
  start a new print; a printer that just `FAILED` a job can.

- `DEGRADED` is the fail-loud state: a telemetry message arrived but
  couldn't be modeled. Not a fallback — an explicit visible signal.

- `OFFLINE` is reached when the heartbeat expires. Determined externally
  (by a heartbeat watcher) and fed in via `HeartbeatMissedEvent`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal


class PrinterState(str, Enum):
    """Canonical printer state across all vendors.

    Distinctions that matter (and that legacy collapsed):
    - `FAILED` ≠ `IDLE`: a print just failed; printer is not ready for new work yet.
    - `FINISHED` ≠ `IDLE`: a print just completed; awaiting ack/clear-the-bed.
    - `ERROR` ≠ `FAILED`: hardware/firmware issue, not a print-job outcome.
    - `DEGRADED`: we received telemetry we could not model. Fail-loud state.
    """

    IDLE = "idle"
    PREPARING = "preparing"       # warming up, homing, loading filament
    PRINTING = "printing"
    PAUSED = "paused"             # user or automatic pause (filament runout, AMS swap)
    FINISHED = "finished"         # print complete, awaiting ack
    FAILED = "failed"             # print ended abnormally
    ERROR = "error"               # printer hardware/firmware error state
    OFFLINE = "offline"           # no recent telemetry
    DEGRADED = "degraded"         # telemetry arrived but could not be modeled


# ===== ActiveError =====

ErrorSource = Literal["hms", "print_error", "webhook"]


@dataclass(frozen=True)
class ActiveError:
    """One active, unresolved error on the printer.

    Errors dedup on `(source, code)`. When seen again, `first_seen_ts`
    stays the same — the earliest occurrence is what matters for
    "this error has been active since ...".
    """

    source: ErrorSource
    code: str                     # e.g. "HMS_05000200_0003000A" or "PRINT_ERROR_257"
    message: str
    first_seen_ts: float
    severity: Literal["info", "warning", "error", "unknown"] = "unknown"


# ===== PrinterStatus =====

@dataclass(frozen=True)
class PrinterStatus:
    """Canonical printer status, constructed by the state-machine transition.

    Frozen to enforce purity in `transition()`. Use `replace(status, ...)`
    to create a new status with updates.
    """

    state: PrinterState
    last_event_ts: float = 0.0

    progress_percent: float | None = None
    layer_current: int | None = None
    layer_total: int | None = None
    time_remaining_sec: int | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    chamber_temp: float | None = None
    current_file: str | None = None
    job_id: str | None = None                      # subtask_id on Bambu, filename hash on Moonraker
    stage_code: int | None = None                  # granular stage (Bambu stg_cur); None on Moonraker
    firmware_versions: tuple[tuple[str, str], ...] = ()  # ((module_name, sw_ver), ...)
    active_errors: tuple[ActiveError, ...] = ()

    @staticmethod
    def initial() -> "PrinterStatus":
        """The status of a just-registered printer before any telemetry arrives."""
        return PrinterStatus(state=PrinterState.OFFLINE, last_event_ts=0.0)


# ===== Transition events =====

@dataclass(frozen=True)
class StateTransitionEvent:
    """One discrete state change, emitted alongside a status update.

    Snapshot tests compare emitted transition lists against
    expected.json fixtures; this is the unit of comparison.
    """

    ts: float                     # wall-clock of the telemetry event that triggered this
    from_state: PrinterState
    to_state: PrinterState
    reason: str | None = None     # human-friendly cause, e.g. "gcode_state: RUNNING → FAILED"


# ===== Errors =====

class OutOfOrderError(RuntimeError):
    """Raised when an event's ts is older than the current status' last_event_ts.

    The transition machine is monotonic: out-of-order events indicate a
    capture replay error or a bugged upstream. Supervisor drops the event
    after logging.
    """


class UnhandledEventError(RuntimeError):
    """Raised when `transition()` receives an event type it doesn't know.

    This is fail-loud. There is no fallback behavior; adding a new event
    kind means adding a new case to the match statement.
    """


class UnknownStageError(RuntimeError):
    """Raised when a Bambu `stg_cur` value is not in the known stage map.

    Legacy STAGE_MAP (in `monitors/mqtt_telemetry.py`) covers 0-14 +
    255 + -1 but the captures show 29 and 39 in use on H2D recovery.
    The V2 mapping is the complete observed set; values outside it
    raise so the observer surfaces the gap.
    """
