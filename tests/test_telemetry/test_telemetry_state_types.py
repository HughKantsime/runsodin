"""Contract tests for state-machine types (T2.1, T2.2, T2.3, T2.4).

These cover the type definitions only — the `transition()` function
itself is tested in test_telemetry_transition.py (T2.5–T2.7).
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    HeartbeatMissedEvent,
    TelemetryEvent,
)
from backend.modules.printers.telemetry.bambu.raw import (
    BambuInfoSection,
    BambuPrintSection,
)
from backend.modules.printers.telemetry.state import (
    ActiveError,
    PrinterState,
    PrinterStatus,
    StateTransitionEvent,
)


class TestPrinterStateEnum:
    def test_has_nine_members(self):
        """The distinct states that legacy collapsed."""
        assert len(PrinterState) == 9

    def test_failed_distinct_from_idle(self):
        assert PrinterState.FAILED != PrinterState.IDLE
        assert PrinterState.FAILED.value == "failed"
        assert PrinterState.IDLE.value == "idle"

    def test_finished_distinct_from_idle(self):
        assert PrinterState.FINISHED != PrinterState.IDLE
        assert PrinterState.FINISHED.value == "finished"

    def test_error_distinct_from_failed(self):
        """Printer hardware error ≠ print job ended abnormally."""
        assert PrinterState.ERROR != PrinterState.FAILED

    def test_degraded_present(self):
        """Fail-loud state for unmodellable telemetry."""
        assert PrinterState.DEGRADED == "degraded"

    def test_all_members_are_strings(self):
        """str Enum — legacy code comparing state == 'idle' keeps working."""
        for m in PrinterState:
            assert isinstance(m.value, str)
            assert m == m.value


class TestPrinterStatusImmutability:
    def test_frozen(self):
        s = PrinterStatus(state=PrinterState.IDLE)
        with pytest.raises(FrozenInstanceError):
            s.state = PrinterState.PRINTING  # type: ignore[misc]

    def test_replace_returns_new_instance(self):
        s1 = PrinterStatus(state=PrinterState.IDLE, last_event_ts=100.0)
        s2 = replace(s1, state=PrinterState.PRINTING)
        assert s1.state == PrinterState.IDLE
        assert s2.state == PrinterState.PRINTING
        assert s1 is not s2
        assert s1.last_event_ts == s2.last_event_ts  # unchanged

    def test_initial_factory(self):
        s = PrinterStatus.initial()
        assert s.state == PrinterState.OFFLINE
        assert s.last_event_ts == 0.0
        assert s.active_errors == ()
        assert s.firmware_versions == ()

    def test_firmware_versions_is_tuple(self):
        """Tuple, not list, for hashability + immutability."""
        s = PrinterStatus(
            state=PrinterState.IDLE,
            firmware_versions=(("ota", "01.09.00.00"), ("mc", "01.03.00.00")),
        )
        assert isinstance(s.firmware_versions, tuple)
        # mutating would fail (can't mutate tuples or frozen dataclasses)

    def test_active_errors_is_tuple(self):
        s = PrinterStatus(
            state=PrinterState.IDLE,
            active_errors=(
                ActiveError(
                    source="hms",
                    code="HMS_05000200_0003000A",
                    message="unknown",
                    first_seen_ts=100.0,
                ),
            ),
        )
        assert isinstance(s.active_errors, tuple)


class TestActiveError:
    def test_frozen(self):
        e = ActiveError(source="hms", code="x", message="y", first_seen_ts=1.0)
        with pytest.raises(FrozenInstanceError):
            e.code = "z"  # type: ignore[misc]

    def test_hashable(self):
        """Frozen dataclasses are hashable — required for set-based dedup."""
        e1 = ActiveError(source="hms", code="x", message="y", first_seen_ts=1.0)
        e2 = ActiveError(source="hms", code="x", message="y", first_seen_ts=1.0)
        assert hash(e1) == hash(e2)
        assert {e1, e2} == {e1}

    def test_dedup_via_source_code(self):
        """The state machine will dedup on (source, code). Two ActiveErrors
        with same source+code but different messages hash differently — that's
        by design. The state machine's dedup is by (source, code) tuple, not
        by ActiveError equality."""
        e1 = ActiveError(source="hms", code="x", message="original", first_seen_ts=1.0)
        e2 = ActiveError(source="hms", code="x", message="updated", first_seen_ts=2.0)
        # Different ActiveErrors (different message + ts).
        assert e1 != e2
        # State-machine dedup key is (source, code):
        assert (e1.source, e1.code) == (e2.source, e2.code)


class TestTelemetryEventUnion:
    """Sealed union — every event subtype is a frozen dataclass, carries ts + printer_id."""

    def test_bambu_report_event(self):
        section = BambuPrintSection.model_validate({"gcode_state": "RUNNING"})
        e = BambuReportEvent(printer_id="h2d-01", ts=100.0, section=section)
        assert e.printer_id == "h2d-01"
        assert e.ts == 100.0
        with pytest.raises(FrozenInstanceError):
            e.ts = 200.0  # type: ignore[misc]

    def test_bambu_info_event(self):
        section = BambuInfoSection.model_validate({
            "command": "get_version",
            "module": [],
        })
        e = BambuInfoEvent(printer_id="h2d-01", ts=50.0, section=section)
        assert e.section.command == "get_version"

    def test_connection_event(self):
        e = ConnectionEvent(printer_id="h2d-01", ts=1.0, kind="connected")
        assert e.kind == "connected"
        assert e.detail is None

    def test_connection_event_with_detail(self):
        e = ConnectionEvent(
            printer_id="h2d-01",
            ts=1.0,
            kind="error",
            detail="ConnectionRefusedError(61, 'Connection refused')",
        )
        assert "ConnectionRefusedError" in e.detail

    def test_heartbeat_missed(self):
        e = HeartbeatMissedEvent(printer_id="h2d-01", ts=200.0, last_seen_ts=100.0)
        assert e.ts - e.last_seen_ts == 100.0

    def test_degraded_event(self):
        e = DegradedEvent(
            printer_id="h2d-01",
            ts=1.0,
            reason="gcode_state='CRASHED' not a member of BambuGcodeState",
            raw_excerpt='{"print":{"gcode_state":"CRASHED"}}',
        )
        assert "CRASHED" in e.reason


class TestStateTransitionEvent:
    def test_frozen(self):
        t = StateTransitionEvent(
            ts=1.0,
            from_state=PrinterState.IDLE,
            to_state=PrinterState.PREPARING,
        )
        with pytest.raises(FrozenInstanceError):
            t.to_state = PrinterState.PRINTING  # type: ignore[misc]

    def test_reason_optional(self):
        t = StateTransitionEvent(
            ts=1.0,
            from_state=PrinterState.IDLE,
            to_state=PrinterState.PREPARING,
        )
        assert t.reason is None

    def test_reason_present(self):
        t = StateTransitionEvent(
            ts=1.0,
            from_state=PrinterState.PRINTING,
            to_state=PrinterState.FAILED,
            reason="gcode_state: RUNNING → FAILED",
        )
        assert "RUNNING" in t.reason
