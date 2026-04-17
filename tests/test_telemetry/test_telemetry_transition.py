"""Contract tests for the pure state-machine transition function
(T2.5, T2.6, T2.7).

Covers:
- Bambu → canonical state mapping (including FAILED ≠ IDLE, FINISHED ≠ IDLE)
- Stage validation (29 and 39 accepted; unknown raises)
- HMS event merging + dedup
- print_error overlay
- Connection + heartbeat + degraded events
- Purity (no mutation of prev, deterministic output)
- Monotonicity + out-of-order rejection
- Unhandled event type raises
"""
from __future__ import annotations

import copy

import pytest

from backend.modules.printers.telemetry.bambu.raw import (
    BambuInfoSection,
    BambuPrintSection,
)
from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    HeartbeatMissedEvent,
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
from backend.modules.printers.telemetry.transition import transition


def _event(section_dict, ts=100.0, printer_id="h2d-01"):
    """Helper: build a BambuReportEvent from a print-section dict."""
    section = BambuPrintSection.model_validate(section_dict)
    return BambuReportEvent(printer_id=printer_id, ts=ts, section=section)


# ===== Bambu state mapping (T2.6) =====

class TestBambuStateMapping:
    def test_idle(self):
        prev = PrinterStatus.initial()
        new, trans = transition(prev, _event({"gcode_state": "IDLE"}))
        assert new.state == PrinterState.IDLE
        assert len(trans) == 1
        assert trans[0].from_state == PrinterState.OFFLINE
        assert trans[0].to_state == PrinterState.IDLE

    def test_prepare(self):
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=50.0)
        new, _ = transition(prev, _event({"gcode_state": "PREPARE"}))
        assert new.state == PrinterState.PREPARING

    def test_running(self):
        prev = PrinterStatus(state=PrinterState.PREPARING, last_event_ts=50.0)
        new, _ = transition(prev, _event({"gcode_state": "RUNNING", "stg_cur": 14}))
        assert new.state == PrinterState.PRINTING

    def test_pause(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        new, _ = transition(prev, _event({"gcode_state": "PAUSE"}))
        assert new.state == PrinterState.PAUSED

    def test_failed_distinct_from_idle(self):
        """The headline bug fix — failed is visible, not silently mapped to IDLE."""
        prev = PrinterStatus(state=PrinterState.PAUSED, last_event_ts=50.0)
        new, trans = transition(prev, _event({"gcode_state": "FAILED"}))
        assert new.state == PrinterState.FAILED
        assert new.state != PrinterState.IDLE
        assert trans[0].from_state == PrinterState.PAUSED
        assert trans[0].to_state == PrinterState.FAILED
        assert "FAILED" in trans[0].reason

    def test_finish_distinct_from_idle(self):
        """Second half of the headline bug — finish is distinct too."""
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        new, _ = transition(prev, _event({"gcode_state": "FINISH"}))
        assert new.state == PrinterState.FINISHED
        assert new.state != PrinterState.IDLE


class TestH2DFailureRecoveryArc:
    """The actual h2d capture sequence: FINISH → PREPARE → RUNNING →
    PAUSE → FAILED → PREPARE → RUNNING → FINISH. Must produce 7
    distinct transitions — legacy adapter would have flattened to just
    2 (IDLE → PRINTING → IDLE)."""

    def test_full_arc_transitions(self):
        prev = PrinterStatus.initial()
        sequence = [
            ({"gcode_state": "FINISH"}, 1.0, PrinterState.FINISHED),
            ({"gcode_state": "PREPARE"}, 2.0, PrinterState.PREPARING),
            ({"gcode_state": "RUNNING", "stg_cur": 2}, 3.0, PrinterState.PRINTING),
            ({"gcode_state": "PAUSE"}, 4.0, PrinterState.PAUSED),
            ({"gcode_state": "FAILED"}, 5.0, PrinterState.FAILED),
            ({"gcode_state": "PREPARE"}, 6.0, PrinterState.PREPARING),
            ({"gcode_state": "RUNNING", "stg_cur": 14}, 7.0, PrinterState.PRINTING),
            ({"gcode_state": "FINISH"}, 8.0, PrinterState.FINISHED),
        ]
        all_transitions: list[StateTransitionEvent] = []
        for section_dict, ts, expected_state in sequence:
            prev, transitions = transition(prev, _event(section_dict, ts=ts))
            assert prev.state == expected_state
            all_transitions.extend(transitions)

        states = [t.to_state for t in all_transitions]
        # We expect 8 transitions (every step was a distinct new state)
        assert len(all_transitions) == 8
        assert PrinterState.FAILED in states
        assert PrinterState.FINISHED in states


class TestStageValidation:
    def test_observed_stages_accepted(self):
        """All 10 stages observed in run-2026-04-16."""
        for stg in [-1, 1, 2, 3, 4, 13, 14, 29, 39, 255]:
            prev = PrinterStatus.initial()
            new, _ = transition(prev, _event({"gcode_state": "RUNNING", "stg_cur": stg}))
            assert new.stage_code == stg
            assert new.state == PrinterState.PRINTING

    def test_stage_29_works_legacy_stagemap_did_not(self):
        """Legacy STAGE_MAP in mqtt_telemetry.py only had 0-14, 255, -1 —
        stage 29 (filament unload during H2D recovery) would have been
        'Stage 29' fallback. V2 accepts it cleanly."""
        prev = PrinterStatus.initial()
        new, _ = transition(prev, _event({"gcode_state": "RUNNING", "stg_cur": 29}))
        assert new.stage_code == 29
        # state is PRINTING, not "Stage 29"

    def test_unknown_stage_fails_loud(self):
        prev = PrinterStatus.initial()
        with pytest.raises(UnknownStageError):
            transition(prev, _event({"gcode_state": "RUNNING", "stg_cur": 999}))


class TestPrintErrorOverlay:
    def test_nonzero_print_error_forces_error_state(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        new, trans = transition(prev, _event({
            "gcode_state": "RUNNING",
            "print_error": 257,  # synthetic non-zero
        }))
        assert new.state == PrinterState.ERROR
        assert any("print_error" in (t.reason or "") for t in trans)
        # and an ActiveError was recorded
        print_errors = [e for e in new.active_errors if e.source == "print_error"]
        assert len(print_errors) == 1
        assert print_errors[0].severity == "error"

    def test_zero_print_error_no_overlay(self):
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=50.0)
        new, _ = transition(prev, _event({
            "gcode_state": "RUNNING",
            "stg_cur": 14,
            "print_error": 0,
        }))
        assert new.state == PrinterState.PRINTING


class TestHMSMerging:
    def test_new_hms_adds_active_error(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        new, _ = transition(prev, _event({
            "gcode_state": "RUNNING",
            "stg_cur": 14,
            "hms": [{"attr": 83886592, "code": 196618}],  # real h2d code
        }))
        hms_errors = [e for e in new.active_errors if e.source == "hms"]
        assert len(hms_errors) == 1
        assert "HMS_05000200_0003000A" in hms_errors[0].code

    def test_repeated_hms_dedups(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        ev = _event({
            "gcode_state": "RUNNING",
            "stg_cur": 14,
            "hms": [{"attr": 83886592, "code": 196618}],
        }, ts=100.0)
        s1, _ = transition(prev, ev)
        s2, _ = transition(s1, _event({
            "gcode_state": "RUNNING",
            "stg_cur": 14,
            "hms": [{"attr": 83886592, "code": 196618}],  # same HMS
        }, ts=101.0))
        # still one active error
        hms_errors = [e for e in s2.active_errors if e.source == "hms"]
        assert len(hms_errors) == 1
        # first_seen_ts must stay the first one
        assert hms_errors[0].first_seen_ts == 100.0

    def test_unknown_hms_code_marked_unknown(self):
        prev = PrinterStatus.initial()
        new, _ = transition(prev, _event({
            "gcode_state": "RUNNING", "stg_cur": 14,
            "hms": [{"attr": 0xDEADBEEF, "code": 0xCAFEBABE}],
        }))
        hms_errors = [e for e in new.active_errors if e.source == "hms"]
        assert len(hms_errors) == 1
        assert hms_errors[0].severity == "unknown"
        assert "UNKNOWN HMS" in hms_errors[0].message


# ===== Non-Bambu events =====

class TestConnectionEvent:
    def test_connected_from_offline_goes_idle(self):
        prev = PrinterStatus.initial()  # OFFLINE
        e = ConnectionEvent(printer_id="h2d-01", ts=1.0, kind="connected")
        new, trans = transition(prev, e)
        assert new.state == PrinterState.IDLE
        assert len(trans) == 1

    def test_connected_while_printing_no_change(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        e = ConnectionEvent(printer_id="h2d-01", ts=51.0, kind="connected")
        new, trans = transition(prev, e)
        assert new.state == PrinterState.PRINTING
        assert trans == []

    def test_disconnected_goes_offline(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        e = ConnectionEvent(printer_id="h2d-01", ts=51.0, kind="disconnected")
        new, trans = transition(prev, e)
        assert new.state == PrinterState.OFFLINE
        assert len(trans) == 1

    def test_error_goes_offline(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        e = ConnectionEvent(
            printer_id="h2d-01", ts=51.0, kind="error",
            detail="ConnectionRefusedError",
        )
        new, trans = transition(prev, e)
        assert new.state == PrinterState.OFFLINE
        assert "ConnectionRefusedError" in trans[0].reason


class TestHeartbeatMissed:
    def test_heartbeat_miss_goes_offline(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        e = HeartbeatMissedEvent(printer_id="h2d-01", ts=200.0, last_seen_ts=50.0)
        new, trans = transition(prev, e)
        assert new.state == PrinterState.OFFLINE
        assert len(trans) == 1


class TestDegradedEvent:
    def test_degraded_transition(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        e = DegradedEvent(
            printer_id="h2d-01", ts=60.0,
            reason="gcode_state='CRASHED' not in enum",
            raw_excerpt='{"print":{"gcode_state":"CRASHED"}}',
        )
        new, trans = transition(prev, e)
        assert new.state == PrinterState.DEGRADED
        assert trans[0].reason == "gcode_state='CRASHED' not in enum"


class TestBambuInfoEvent:
    def test_info_updates_firmware_versions_no_state_change(self):
        prev = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=50.0)
        section = BambuInfoSection.model_validate({
            "command": "get_version",
            "module": [
                {"name": "ota", "sw_ver": "01.09.00.00"},
                {"name": "mc", "sw_ver": "01.03.00.00"},
            ],
        })
        e = BambuInfoEvent(printer_id="h2d-01", ts=51.0, section=section)
        new, trans = transition(prev, e)
        assert new.state == PrinterState.PRINTING  # unchanged
        assert trans == []
        assert ("ota", "01.09.00.00") in new.firmware_versions
        assert ("mc", "01.03.00.00") in new.firmware_versions


# ===== Purity / determinism (T2.7) =====

class TestPurity:
    def test_identical_input_identical_output(self):
        """Called twice with same (prev, event), returns byte-identical output."""
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=50.0)
        ev = _event({"gcode_state": "RUNNING", "stg_cur": 14, "mc_percent": 50}, ts=100.0)

        r1 = transition(prev, ev)
        r2 = transition(prev, ev)
        assert r1 == r2
        assert r1[0] == r2[0]
        assert r1[1] == r2[1]

    def test_prev_not_mutated(self):
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=50.0)
        prev_snapshot = copy.deepcopy(prev)
        ev = _event({"gcode_state": "RUNNING", "stg_cur": 14}, ts=100.0)
        transition(prev, ev)
        assert prev == prev_snapshot  # unchanged

    def test_event_not_mutated(self):
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=50.0)
        ev = _event({"gcode_state": "RUNNING", "stg_cur": 14}, ts=100.0)
        ev_snapshot = copy.deepcopy(ev)
        transition(prev, ev)
        # can't use == directly because section holds a Pydantic model (not deep-equal-clean);
        # compare the timestamps and section's model_dump instead
        assert ev.ts == ev_snapshot.ts
        assert ev.printer_id == ev_snapshot.printer_id
        assert ev.section.model_dump() == ev_snapshot.section.model_dump()

    def test_no_wall_clock_calls(self):
        """AST-level check: transition module has no time.time()/datetime.now()
        calls and does not import `time` or `datetime`. Docstrings that
        reference these by name don't count."""
        import ast
        import inspect
        import backend.modules.printers.telemetry.transition as m

        tree = ast.parse(inspect.getsource(m))

        # No `import time` or `from time import ...`
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "time", "transition module imports time"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "time", "transition module imports from time"
                # datetime would leak if it were imported too
                assert node.module != "datetime", "transition module imports from datetime"

        # No `time.time()` or `datetime.now(...)` calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        name = f"{func.value.id}.{func.attr}"
                        assert name not in ("time.time", "datetime.now"), (
                            f"transition calls {name}() — violates purity contract"
                        )


class TestMonotonicity:
    def test_out_of_order_raises(self):
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=100.0)
        ev = _event({"gcode_state": "RUNNING", "stg_cur": 14}, ts=50.0)  # ts < prev
        with pytest.raises(OutOfOrderError):
            transition(prev, ev)

    def test_equal_ts_accepted(self):
        """Events at the same ts are accepted — multiple adapters may share a clock tick."""
        prev = PrinterStatus(state=PrinterState.IDLE, last_event_ts=100.0)
        ev = _event({"gcode_state": "RUNNING", "stg_cur": 14}, ts=100.0)
        new, _ = transition(prev, ev)
        assert new.state == PrinterState.PRINTING


class TestUnhandledEvent:
    def test_unknown_event_kind_fails_loud(self):
        prev = PrinterStatus.initial()

        class FakeEvent:
            ts = 100.0

        with pytest.raises(UnhandledEventError):
            transition(prev, FakeEvent())  # type: ignore[arg-type]
