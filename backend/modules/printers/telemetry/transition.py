"""Pure state-machine transition function.

`transition(prev, event)` is the heart of the V2 pipeline. It is a
**pure function**: no I/O, no wall-clock, no random, no hidden state.
Given the same `(prev, event)`, it always returns the same
`(new_status, transitions)` output.

Purity is what makes:
- the replayer deterministic (replaying a slice produces the same
  transitions every time),
- contract tests possible (snapshot `expected.json` files),
- shadow-mode diffing against legacy trustworthy (legacy impurity
  showed up as spurious diffs during development of this module).
"""
from __future__ import annotations

from dataclasses import replace

from backend.modules.printers.telemetry.bambu.enums import BambuGcodeState
from backend.modules.printers.telemetry.bambu.hms import get_catalog
from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection
from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    HeartbeatMissedEvent,
    TelemetryEvent,
)
from backend.modules.printers.telemetry.state import (
    ActiveError,
    OutOfOrderError,
    PrinterState,
    PrinterStatus,
    StateTransitionEvent,
    UnhandledEventError,
    UnknownStageError,
)


# ===== Bambu state mapping (empirically derived) =====

# All gcode_state → canonical state mappings. These are the dominant rule;
# `print_error != 0` and HMS severity=error may overlay ERROR on top.
_BAMBU_STATE_MAP: dict[BambuGcodeState, PrinterState] = {
    BambuGcodeState.IDLE: PrinterState.IDLE,
    BambuGcodeState.PREPARE: PrinterState.PREPARING,
    BambuGcodeState.RUNNING: PrinterState.PRINTING,
    BambuGcodeState.PAUSE: PrinterState.PAUSED,
    BambuGcodeState.FAILED: PrinterState.FAILED,       # distinct from IDLE!
    BambuGcodeState.FINISH: PrinterState.FINISHED,     # distinct from IDLE!
}

# All `stg_cur` values observed in odin-e2e/captures/run-2026-04-16 across
# 4 Bambu printers. Values outside this set raise UnknownStageError (fail
# loud) — legacy STAGE_MAP in monitors/mqtt_telemetry.py covered only
# 0-14, 255, -1 and would render 29/39 as "Stage 29"/"Stage 39" fallback.
# This map is the complete *observed* set; future captures may add more.
_BAMBU_KNOWN_STAGES: set[int] = {
    -1,     # sentinel: just-failed / not-yet-started
    0,      # idle/finished (legacy mqtt_telemetry.STAGE_MAP calls this "Idle")
    1,      # auto-leveling
    2,      # heatbed preheating / first layer
    3,      # sweeping XY
    4,      # changing filament
    13,     # first-layer check
    14,     # printing (the main running stage)
    29,     # filament unload (observed in H2D recovery; NOT in legacy STAGE_MAP)
    39,     # post-print sequence (observed in H2D recovery; NOT in legacy STAGE_MAP)
    255,    # sentinel: idle / between-stages
}


# ===== Public entry point =====

def transition(
    prev: PrinterStatus,
    event: TelemetryEvent,
) -> tuple[PrinterStatus, list[StateTransitionEvent]]:
    """Apply an event to a status, return the new status + any transitions.

    Contract:
    - **Pure.** No side effects. No `time.time()`. No random. No I/O.
    - **Monotonic.** `event.ts` must be ≥ `prev.last_event_ts`, else
      raises `OutOfOrderError`.
    - **Exhaustive.** Every `TelemetryEvent` subclass is handled; an
      unknown kind raises `UnhandledEventError` (fail loud).
    - **Immutable input.** `prev` is never mutated. Callers may reuse it.
    """
    if event.ts < prev.last_event_ts:
        raise OutOfOrderError(
            f"event ts {event.ts} < prev.last_event_ts {prev.last_event_ts}"
        )

    match event:
        case BambuReportEvent():
            return _apply_bambu_report(prev, event)
        case BambuInfoEvent():
            return _apply_bambu_info(prev, event)
        case ConnectionEvent():
            return _apply_connection(prev, event)
        case HeartbeatMissedEvent():
            return _apply_heartbeat_missed(prev, event)
        case DegradedEvent():
            return _apply_degraded(prev, event)
        case _:
            raise UnhandledEventError(f"unknown TelemetryEvent kind: {type(event).__name__}")


# ===== Bambu report (the 99% case) =====

def _apply_bambu_report(
    prev: PrinterStatus,
    event: BambuReportEvent,
) -> tuple[PrinterStatus, list[StateTransitionEvent]]:
    section = event.section

    # ---- derive the new canonical state ----
    new_state: PrinterState = prev.state
    reason_parts: list[str] = []

    if section.gcode_state is not None:
        mapped = _BAMBU_STATE_MAP.get(section.gcode_state)
        if mapped is None:
            # enum validation at parse-time should have prevented this
            raise UnhandledEventError(
                f"BambuGcodeState {section.gcode_state} has no canonical mapping"
            )
        new_state = mapped
        if prev.state != new_state:
            reason_parts.append(f"gcode_state={section.gcode_state.value}")

    # stage validation — fail loud on unknown stg_cur
    if section.stg_cur is not None and section.stg_cur not in _BAMBU_KNOWN_STAGES:
        raise UnknownStageError(
            f"stg_cur={section.stg_cur} not in known map. "
            f"Add to _BAMBU_KNOWN_STAGES after confirming with captures."
        )

    # print_error overlays ERROR — regardless of gcode_state
    if section.print_error is not None and section.print_error != 0:
        new_state = PrinterState.ERROR
        reason_parts.append(f"print_error={section.print_error}")

    # ---- active_errors: merge HMS into the active set, dedup by (source, code) ----
    new_errors = _merge_hms_errors(prev.active_errors, section, event.ts)

    # if any active error has severity=error, overlay ERROR
    if any(e.severity == "error" for e in new_errors):
        if new_state not in (PrinterState.ERROR,):
            reason_parts.append("hms_severity=error")
            new_state = PrinterState.ERROR

    # ---- construct new status ----
    new_status = PrinterStatus(
        state=new_state,
        last_event_ts=event.ts,
        progress_percent=_first_not_none(section.mc_percent, prev.progress_percent),
        layer_current=_first_not_none(section.layer_num, prev.layer_current),
        layer_total=_first_not_none(section.total_layer_num, prev.layer_total),
        time_remaining_sec=_convert_minutes_to_seconds(section.mc_remaining_time)
            if section.mc_remaining_time is not None else prev.time_remaining_sec,
        bed_temp=_first_not_none(section.bed_temper, prev.bed_temp),
        bed_target=_first_not_none(section.bed_target_temper, prev.bed_target),
        nozzle_temp=_first_not_none(section.nozzle_temper, prev.nozzle_temp),
        nozzle_target=_first_not_none(section.nozzle_target_temper, prev.nozzle_target),
        chamber_temp=_first_not_none(section.chamber_temper, prev.chamber_temp),
        current_file=_first_not_none(section.gcode_file, prev.current_file),
        job_id=_first_not_none(section.subtask_id, prev.job_id),
        stage_code=_first_not_none(section.stg_cur, prev.stage_code),
        firmware_versions=prev.firmware_versions,
        active_errors=new_errors,
    )

    # ---- build transitions list ----
    transitions: list[StateTransitionEvent] = []
    if new_status.state != prev.state:
        transitions.append(StateTransitionEvent(
            ts=event.ts,
            from_state=prev.state,
            to_state=new_status.state,
            reason=", ".join(reason_parts) if reason_parts else None,
        ))

    return new_status, transitions


def _merge_hms_errors(
    prev_errors: tuple[ActiveError, ...],
    section: BambuPrintSection,
    ts: float,
) -> tuple[ActiveError, ...]:
    """Merge new HMS entries into the prev active_errors tuple.

    Dedup key: (source, code). If an HMS code is already active, keep the
    original `first_seen_ts`. If an HMS code is no longer in `section.hms`,
    it stays in active_errors until explicitly cleared by a subsequent
    report with a non-error print_error==0 and no HMS entries (that's a
    design choice — Bambu firmware doesn't emit explicit 'cleared' events).

    This function never returns duplicates.
    """
    # Keep existing errors indexed by (source, code) for O(1) lookup.
    by_key: dict[tuple[str, str], ActiveError] = {
        (e.source, e.code): e for e in prev_errors
    }

    catalog = get_catalog()

    for hms in section.hms:
        key = hms.key
        source = "hms"
        code = f"HMS_{key}"
        lookup = catalog.lookup(hms)
        if lookup is not None:
            severity = lookup.severity
            message = lookup.message
        else:
            severity = "unknown"
            message = f"UNKNOWN HMS code {key} — not in catalog"

        existing = by_key.get((source, code))
        if existing is not None:
            # dedup — keep original first_seen_ts
            continue
        by_key[(source, code)] = ActiveError(
            source=source,
            code=code,
            message=message,
            first_seen_ts=ts,
            severity=severity,
        )

    # print_error → a synthetic ActiveError (uses its own source/key)
    if section.print_error is not None and section.print_error != 0:
        source = "print_error"
        code = f"PRINT_ERROR_{section.print_error}"
        existing = by_key.get((source, code))
        if existing is None:
            by_key[(source, code)] = ActiveError(
                source=source,
                code=code,
                message=section.mc_print_error_code or f"Bambu print_error={section.print_error}",
                first_seen_ts=ts,
                severity="error",
            )

    # return in a stable order for determinism
    return tuple(sorted(by_key.values(), key=lambda e: (e.source, e.code)))


# ===== Info / connection / heartbeat / degraded handlers =====

def _apply_bambu_info(
    prev: PrinterStatus,
    event: BambuInfoEvent,
) -> tuple[PrinterStatus, list[StateTransitionEvent]]:
    """Info events update firmware_versions only; no state change."""
    versions = tuple(
        sorted(
            (m.name, m.sw_ver) for m in event.section.module
        )
    )
    new_status = replace(prev, firmware_versions=versions, last_event_ts=event.ts)
    return new_status, []


def _apply_connection(
    prev: PrinterStatus,
    event: ConnectionEvent,
) -> tuple[PrinterStatus, list[StateTransitionEvent]]:
    """Connection events drive OFFLINE transitions.

    - `connected` while OFFLINE → move to IDLE (tentative; the next
      telemetry message will snap us to the real state).
    - `connected` while not OFFLINE → no state change (already online).
    - `disconnected` or `error` → OFFLINE, regardless of prev state.
    """
    transitions: list[StateTransitionEvent] = []
    if event.kind == "connected":
        if prev.state == PrinterState.OFFLINE:
            new_state = PrinterState.IDLE
            transitions.append(StateTransitionEvent(
                ts=event.ts,
                from_state=prev.state,
                to_state=new_state,
                reason="connected (awaiting telemetry)",
            ))
        else:
            new_state = prev.state
    else:  # disconnected | error
        new_state = PrinterState.OFFLINE
        if prev.state != new_state:
            transitions.append(StateTransitionEvent(
                ts=event.ts,
                from_state=prev.state,
                to_state=new_state,
                reason=f"connection {event.kind}: {event.detail or ''}".strip(),
            ))

    new_status = replace(prev, state=new_state, last_event_ts=event.ts)
    return new_status, transitions


def _apply_heartbeat_missed(
    prev: PrinterStatus,
    event: HeartbeatMissedEvent,
) -> tuple[PrinterStatus, list[StateTransitionEvent]]:
    """Heartbeat watcher says we haven't heard from this printer → OFFLINE."""
    transitions: list[StateTransitionEvent] = []
    if prev.state != PrinterState.OFFLINE:
        transitions.append(StateTransitionEvent(
            ts=event.ts,
            from_state=prev.state,
            to_state=PrinterState.OFFLINE,
            reason=f"heartbeat missed (last seen {event.last_seen_ts})",
        ))
    new_status = replace(prev, state=PrinterState.OFFLINE, last_event_ts=event.ts)
    return new_status, transitions


def _apply_degraded(
    prev: PrinterStatus,
    event: DegradedEvent,
) -> tuple[PrinterStatus, list[StateTransitionEvent]]:
    """Unmodellable telemetry → DEGRADED (fail-loud visible state)."""
    transitions: list[StateTransitionEvent] = []
    if prev.state != PrinterState.DEGRADED:
        transitions.append(StateTransitionEvent(
            ts=event.ts,
            from_state=prev.state,
            to_state=PrinterState.DEGRADED,
            reason=event.reason,
        ))
    new_status = replace(prev, state=PrinterState.DEGRADED, last_event_ts=event.ts)
    return new_status, transitions


# ===== Helpers =====

def _first_not_none(*values):
    """Return the first non-None arg, or None if all are None."""
    for v in values:
        if v is not None:
            return v
    return None


def _convert_minutes_to_seconds(minutes: int | None) -> int | None:
    """Bambu's `mc_remaining_time` is minutes; `PrinterStatus.time_remaining_sec` is seconds."""
    if minutes is None:
        return None
    return minutes * 60
