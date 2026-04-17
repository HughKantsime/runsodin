"""Contract tests for BambuV2StatusView (Phase 2 T2.1-T2.2).

Ensures the legacy-shaped accessor surface matches what routes expect
and that V2's canonical PrinterStatus projects cleanly onto it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.modules.printers.telemetry.bambu.status_view import (
    AMSSlotCompat,
    BambuV2StatusView,
    ams_slots_from_section,
)
from backend.modules.printers.telemetry.replay import replay
from backend.modules.printers.telemetry.state import (
    ActiveError,
    PrinterState,
    PrinterStatus,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"


class TestStateMapping:
    @pytest.mark.parametrize("v2_state, legacy_state", [
        (PrinterState.IDLE, "idle"),
        (PrinterState.PRINTING, "printing"),
        (PrinterState.PREPARING, "printing"),
        (PrinterState.PAUSED, "paused"),
        (PrinterState.ERROR, "error"),
        (PrinterState.OFFLINE, "offline"),
        (PrinterState.FINISHED, "idle"),
        (PrinterState.FAILED, "idle"),
        (PrinterState.DEGRADED, "unknown"),
    ])
    def test_state_projects_to_legacy_enum(self, v2_state, legacy_state):
        s = PrinterStatus(state=v2_state, last_event_ts=1.0)
        view = BambuV2StatusView(s)
        assert view.state == legacy_state

    def test_v2_state_raw_preserves_specifics(self):
        """Even when projected to legacy, V2 state is readable via v2_state_raw."""
        s = PrinterStatus(state=PrinterState.FAILED, last_event_ts=1.0)
        view = BambuV2StatusView(s)
        assert view.state == "idle"
        assert view.v2_state_raw == "failed"


class TestScalarFields:
    def test_all_numeric_fields_default_to_zero_or_empty_when_none(self):
        s = PrinterStatus(state=PrinterState.IDLE, last_event_ts=0.0)
        view = BambuV2StatusView(s)
        assert view.print_progress == 0
        assert view.layer_current == 0
        assert view.layer_total == 0
        assert view.time_remaining_minutes == 0
        assert view.current_file == ""
        assert view.bed_temp == 0.0
        assert view.bed_target == 0.0
        assert view.nozzle_temp == 0.0
        assert view.nozzle_target == 0.0
        assert view.fan_speed == 0
        assert view.error_message == ""

    def test_numeric_fields_passthrough(self):
        s = PrinterStatus(
            state=PrinterState.PRINTING,
            last_event_ts=1.0,
            progress_percent=45.7,
            layer_current=27,
            layer_total=54,
            time_remaining_sec=1800,  # 30 min
            bed_temp=60.5,
            bed_target=60.0,
            nozzle_temp=220.3,
            nozzle_target=220.0,
            current_file="/sdcard/dragon.gcode",
        )
        view = BambuV2StatusView(s)
        assert view.print_progress == 45  # int cast
        assert view.layer_current == 27
        assert view.layer_total == 54
        assert view.time_remaining_minutes == 30  # seconds / 60
        assert view.bed_temp == 60.5
        assert view.bed_target == 60.0
        assert view.nozzle_temp == 220.3
        assert view.nozzle_target == 220.0
        assert view.current_file == "/sdcard/dragon.gcode"


class TestErrorMessageConcatenation:
    def test_empty_when_no_errors(self):
        s = PrinterStatus(state=PrinterState.PRINTING, last_event_ts=0.0)
        view = BambuV2StatusView(s)
        assert view.error_message == ""

    def test_single_error_message(self):
        s = PrinterStatus(
            state=PrinterState.ERROR,
            last_event_ts=1.0,
            active_errors=(
                ActiveError(
                    source="hms",
                    code="HMS_x",
                    message="AMS humidity warning",
                    first_seen_ts=0.5,
                ),
            ),
        )
        view = BambuV2StatusView(s)
        assert view.error_message == "AMS humidity warning"

    def test_multiple_errors_concatenated(self):
        s = PrinterStatus(
            state=PrinterState.ERROR,
            last_event_ts=1.0,
            active_errors=(
                ActiveError(source="hms", code="HMS_1", message="First",
                            first_seen_ts=0.5),
                ActiveError(source="print_error", code="PRINT_ERROR_257",
                            message="Second", first_seen_ts=0.6),
            ),
        )
        view = BambuV2StatusView(s)
        # sorted by (source, code) — hms_1 first, print_error second
        assert "First" in view.error_message
        assert "Second" in view.error_message
        assert "; " in view.error_message


class TestAMSSlotsFromSection:
    def test_empty_section(self):
        assert ams_slots_from_section(None) == []

    def test_fixture_ams_projection(self):
        """Replay a fixture, grab the last BambuPrintSection, project its AMS."""
        from backend.modules.printers.telemetry.replay import iter_events
        from backend.modules.printers.telemetry.events import BambuReportEvent

        last_section = None
        for event in iter_events(FIXTURES / "bambu-x1c-ams-swap.jsonl"):
            if isinstance(event, BambuReportEvent):
                last_section = event.section
        assert last_section is not None, "no BambuReportEvents in fixture"

        slots = ams_slots_from_section(last_section)
        # x1c has 1 AMS unit × 4 trays
        assert len(slots) >= 1
        assert all(isinstance(s, AMSSlotCompat) for s in slots)
        # slot_number is 1-indexed
        assert slots[0].slot_number == 1


class TestFixtureIntegration:
    """End-to-end: replay a fixture through V2, wrap in view, read fields."""

    @pytest.mark.parametrize("fixture, expected_legacy_state", [
        ("bambu-a1-kickoff", "printing"),         # final V2 state PRINTING
        ("bambu-a1-happy-path", "idle"),          # final V2 state FINISHED → legacy idle
        ("bambu-h2d-failure", "error"),           # final V2 state ERROR
        ("bambu-h2d-failure-arc", "error"),       # final V2 state ERROR
        ("bambu-h2d-recovery", "error"),          # final V2 state ERROR
        ("bambu-x1c-ams-swap", "printing"),       # final V2 state PRINTING
    ])
    def test_replay_then_view(self, fixture, expected_legacy_state):
        result = replay(FIXTURES / f"{fixture}.jsonl")
        view = BambuV2StatusView(result.final_status)
        assert view.state == expected_legacy_state
