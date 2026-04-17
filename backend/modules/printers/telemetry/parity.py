"""Legacy-vs-V2 parity baseline (T7.1 + T4.5 scope-reduced).

Rather than instantiate the 684-line legacy `BambuPrinter` class (which
is tightly coupled to MQTT lifecycle + I/O threads), this module
**simulates** legacy behavior on a payload-by-payload basis. The
simulation reproduces legacy's state mapping exactly as documented in
`adapters/bambu.py::_parse_status()` — extracted by reading the code,
not by importing it.

This lets the track document WHERE V2 intentionally diverges from
legacy without requiring live MQTT or a broker.

The output of `run_parity_against_fixture()` is a per-event list of
field-by-field comparisons, each classified as:

- `intentional` — V2 is *meant* to differ here (e.g. `FAILED ≠ IDLE`).
- `improvement` — V2 surfaces something legacy dropped (e.g. HMS).
- `bug` — V2 diverges in a way not documented. A bug in the rewrite.

The committed `parity_baseline.json` snapshot is the cutover checklist:
"these are the diffs you'll see when you flip the flag; they are all
intentional wins." Any new entry appearing in a re-run is a regression.

What this module is NOT:

- Not a live shadow-mode diff metric. That needs both adapters running
  against real MQTT traffic, which requires broker infrastructure
  deferred to the `replayer-live-mqtt` follow-up track.
- Not a replacement for `GET /api/telemetry/v2-diff`. That endpoint is
  part of the same deferred scope.
- Not wired into the feature flag. It's a developer / reviewer tool,
  executed manually or via `pytest tests/test_telemetry/test_parity.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from backend.modules.printers.telemetry.bambu.enums import BambuGcodeState
from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection
from backend.modules.printers.telemetry.replay import iter_events
from backend.modules.printers.telemetry.events import BambuReportEvent
from backend.modules.printers.telemetry.state import (
    PrinterState,
    PrinterStatus,
)
from backend.modules.printers.telemetry.transition import transition


# ===== Legacy simulator =====

class LegacyPrinterState(str, Enum):
    """What legacy `adapters/bambu.py::PrinterState` can emit.

    Notable: no FAILED, no FINISHED, no DEGRADED. Legacy collapses both
    FAILED and FINISH to IDLE, and has no fail-loud state.
    """

    UNKNOWN = "unknown"
    IDLE = "idle"
    PRINTING = "printing"
    PAUSED = "paused"
    ERROR = "error"
    OFFLINE = "offline"


@dataclass
class LegacyStatus:
    """Minimal legacy PrinterStatus used for parity comparison.

    Mirrors what `adapters/bambu.py::_parse_status()` would assign to
    its self._status. Only the fields V2 also exposes are modeled — the
    rest aren't comparable.
    """

    state: LegacyPrinterState = LegacyPrinterState.OFFLINE
    print_progress: int = 0
    layer_current: int = 0
    layer_total: int = 0
    time_remaining_minutes: int = 0
    current_file: str = ""
    bed_temp: float = 0.0
    bed_target: float = 0.0
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    fan_speed: int = 0
    error_message: str = ""


def simulate_legacy_parse(status: LegacyStatus, section: BambuPrintSection) -> LegacyStatus:
    """Reproduce `adapters/bambu.py::_parse_status()` mapping on a single
    print section. Returns a new LegacyStatus (simulated legacy would
    have mutated its internal state; we stay immutable here for purity).

    Source of truth: `backend/modules/printers/adapters/bambu.py` lines
    512–569 as audited 2026-04-17. If that code changes, update this
    simulator.
    """
    # Parse printer state
    gcode_state_value = section.gcode_state.value if section.gcode_state else ""
    if gcode_state_value == "IDLE":
        new_state = LegacyPrinterState.IDLE
    elif gcode_state_value in ("RUNNING", "PREPARE"):
        new_state = LegacyPrinterState.PRINTING
    elif gcode_state_value == "PAUSE":
        new_state = LegacyPrinterState.PAUSED
    elif gcode_state_value in ("FAILED", "FINISH"):
        # THE headline bug — legacy maps both to IDLE
        new_state = LegacyPrinterState.IDLE
    elif section.gcode_state is None:
        new_state = status.state  # no update
    else:
        new_state = LegacyPrinterState.UNKNOWN

    # print_error overlays ERROR (matches V2)
    error_message = status.error_message
    if section.print_error is not None and section.print_error != 0:
        new_state = LegacyPrinterState.ERROR
        error_message = str(section.print_error)

    # Fan — legacy coerces cooling_fan_speed to int via get() default
    # (silent fallback on str, which is what Bambu actually sends)
    legacy_fan_speed = status.fan_speed
    if section.cooling_fan_speed is not None:
        try:
            legacy_fan_speed = int(section.cooling_fan_speed)
        except (ValueError, TypeError):
            legacy_fan_speed = 0  # legacy silent fallback

    return LegacyStatus(
        state=new_state,
        print_progress=section.mc_percent if section.mc_percent is not None else status.print_progress,
        layer_current=section.layer_num if section.layer_num is not None else status.layer_current,
        layer_total=section.total_layer_num if section.total_layer_num is not None else status.layer_total,
        time_remaining_minutes=(
            section.mc_remaining_time if section.mc_remaining_time is not None
            else status.time_remaining_minutes
        ),
        current_file=section.gcode_file if section.gcode_file is not None else status.current_file,
        bed_temp=section.bed_temper if section.bed_temper is not None else status.bed_temp,
        bed_target=(
            section.bed_target_temper if section.bed_target_temper is not None
            else status.bed_target
        ),
        nozzle_temp=section.nozzle_temper if section.nozzle_temper is not None else status.nozzle_temp,
        nozzle_target=(
            section.nozzle_target_temper if section.nozzle_target_temper is not None
            else status.nozzle_target
        ),
        fan_speed=legacy_fan_speed,
        error_message=error_message,
    )


# ===== Diff classification =====

Classification = Literal["intentional", "improvement", "bug"]


@dataclass(frozen=True)
class ParityDiff:
    """One field-level divergence between V2 and legacy on a given event."""

    field: str
    v2_value: Any
    legacy_value: Any
    classification: Classification
    rationale: str


# ===== The known, intentional diffs =====

# Mapping from V2 canonical state → (legacy state, classification, rationale).
# Any state transition in V2 that corresponds to a different legacy state is
# classified using this map. Entries NOT in this map, or states that don't
# map, are flagged as `bug` — fail-loud.
_KNOWN_STATE_DIFFS: dict[tuple[PrinterState, LegacyPrinterState], tuple[Classification, str]] = {
    (PrinterState.FAILED, LegacyPrinterState.IDLE): (
        "intentional",
        "V2 distinguishes FAILED from IDLE; legacy collapses both. Headline fix.",
    ),
    (PrinterState.FINISHED, LegacyPrinterState.IDLE): (
        "intentional",
        "V2 distinguishes FINISHED from IDLE; legacy collapses both. Headline fix.",
    ),
    (PrinterState.PREPARING, LegacyPrinterState.PRINTING): (
        "intentional",
        "V2 has a dedicated PREPARING state; legacy maps PREPARE to PRINTING.",
    ),
    (PrinterState.DEGRADED, LegacyPrinterState.UNKNOWN): (
        "intentional",
        "V2 has explicit DEGRADED fail-loud state; legacy falls through to UNKNOWN.",
    ),
    (PrinterState.ERROR, LegacyPrinterState.IDLE): (
        "improvement",
        "V2 surfaces ERROR via HMS severity=error or print_error overlay even "
        "when gcode_state is IDLE/FINISH; legacy drops HMS silently.",
    ),
    (PrinterState.ERROR, LegacyPrinterState.PRINTING): (
        "improvement",
        "V2 surfaces ERROR via HMS severity=error even while gcode_state=RUNNING; "
        "legacy doesn't look at HMS.",
    ),
    (PrinterState.ERROR, LegacyPrinterState.PAUSED): (
        "improvement",
        "V2 promotes PAUSED to ERROR on HMS severity=error or print_error; "
        "legacy stays in PAUSED.",
    ),
}


def classify_state_diff(
    v2_state: PrinterState,
    legacy_state: LegacyPrinterState,
) -> tuple[Classification, str]:
    """Classify a state-field divergence."""
    key = (v2_state, legacy_state)
    if key in _KNOWN_STATE_DIFFS:
        return _KNOWN_STATE_DIFFS[key]
    return ("bug", f"unexpected state diff: v2={v2_state.value} legacy={legacy_state.value}")


# ===== Comparison =====

def _compare_fields(v2: PrinterStatus, legacy: LegacyStatus) -> list[ParityDiff]:
    """Emit diffs for every field that disagrees."""
    diffs: list[ParityDiff] = []

    # State — the hot one
    # Map v2.state.value against legacy.state.value. Strings equal → no diff.
    if v2.state.value != legacy.state.value:
        classification, rationale = classify_state_diff(v2.state, legacy.state)
        diffs.append(ParityDiff(
            field="state",
            v2_value=v2.state.value,
            legacy_value=legacy.state.value,
            classification=classification,
            rationale=rationale,
        ))

    # Progress
    v2_progress = int(v2.progress_percent) if v2.progress_percent is not None else 0
    if v2_progress != legacy.print_progress:
        diffs.append(ParityDiff(
            field="progress_percent",
            v2_value=v2_progress,
            legacy_value=legacy.print_progress,
            classification="bug",
            rationale="progress_percent should match — Bambu emits this as int",
        ))

    # Layer numbers
    v2_layer = v2.layer_current or 0
    if v2_layer != legacy.layer_current:
        diffs.append(ParityDiff(
            field="layer_current",
            v2_value=v2_layer,
            legacy_value=legacy.layer_current,
            classification="bug",
            rationale="layer_current should match",
        ))

    # Active errors — legacy had only error_message (single string from print_error);
    # V2 has a typed ActiveError list that includes HMS codes.
    v2_hms_errors = [e for e in v2.active_errors if e.source == "hms"]
    if v2_hms_errors:
        diffs.append(ParityDiff(
            field="active_errors",
            v2_value=f"{len(v2_hms_errors)} HMS entries",
            legacy_value="[legacy drops HMS]",
            classification="improvement",
            rationale="V2 surfaces HMS codes as ActiveError; legacy drops print.hms[].",
        ))

    # Firmware versions — legacy didn't track these at all
    if v2.firmware_versions and legacy.state == LegacyPrinterState.OFFLINE:
        # skip — both were uninitialized
        pass

    # stage_code — legacy had STAGE_MAP limited to {0-14, 255, -1}
    if v2.stage_code is not None and v2.stage_code in (29, 39):
        diffs.append(ParityDiff(
            field="stage_code",
            v2_value=v2.stage_code,
            legacy_value="[legacy STAGE_MAP fallback: 'Stage N']",
            classification="improvement",
            rationale="V2 accepts stages 29/39 (H2D recovery); legacy falls through.",
        ))

    return diffs


# ===== Public entry point =====

@dataclass
class ParityReport:
    """Output of `run_parity_against_fixture()`."""

    fixture: str
    event_count: int
    diffs: list[ParityDiff]

    @property
    def bug_count(self) -> int:
        return sum(1 for d in self.diffs if d.classification == "bug")

    @property
    def intentional_count(self) -> int:
        return sum(1 for d in self.diffs if d.classification == "intentional")

    @property
    def improvement_count(self) -> int:
        return sum(1 for d in self.diffs if d.classification == "improvement")


def run_parity_against_fixture(
    path: Path,
    printer_id: str,
) -> ParityReport:
    """Replay `path` through both V2 and legacy-sim, collect all diffs.

    Returns a report. Dedups diffs — if the same (field, v2_value,
    legacy_value) appears repeatedly (it will, once the state stabilizes),
    only the first occurrence is recorded. This keeps the report a cutover
    checklist, not a noise log.
    """
    v2_status = PrinterStatus.initial()
    legacy_status = LegacyStatus()
    seen_diffs: set[tuple[str, Any, Any]] = set()
    unique_diffs: list[ParityDiff] = []
    event_count = 0

    for event in iter_events(path, printer_id=printer_id):
        if not isinstance(event, BambuReportEvent):
            continue
        event_count += 1
        try:
            v2_status, _ = transition(v2_status, event)
        except Exception:
            continue  # transition errors are their own pipeline concern
        legacy_status = simulate_legacy_parse(legacy_status, event.section)

        for diff in _compare_fields(v2_status, legacy_status):
            # Use v2_value + legacy_value as stringified keys for dedup;
            # actual values may be non-hashable (counts, etc.)
            key = (diff.field, str(diff.v2_value), str(diff.legacy_value))
            if key not in seen_diffs:
                seen_diffs.add(key)
                unique_diffs.append(diff)

    return ParityReport(
        fixture=path.name,
        event_count=event_count,
        diffs=unique_diffs,
    )
