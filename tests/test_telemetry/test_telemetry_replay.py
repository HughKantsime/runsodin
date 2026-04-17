"""Contract tests for the in-process replay module (Phase 3).

Replay drives JSONL fixtures through `transition()` and compares the
emitted state-transition timeline against a committed expected.json
snapshot. If the pipeline's behavior drifts, these tests fail —
forcing either a bug fix or an explicit acknowledgment + snapshot
regeneration.

Fixtures live at `tests/fixtures/telemetry/`. They are tiny slices
cut from real captures in `odin-e2e/captures/run-2026-04-16`. Total
fixture budget is ~500KB — under CI's patience threshold.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.events import (
    BambuReportEvent,
    ConnectionEvent,
)
from backend.modules.printers.telemetry.replay import (
    MAX_GAP_SEC,
    bootstrap_expected_states,
    iter_events,
    line_to_event,
    replay,
    slice_capture,
)
from backend.modules.printers.telemetry.state import PrinterState


FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"


class TestLineToEvent:
    def test_bambu_push_status(self):
        line = {
            "ts": 100.0,
            "iso": "2026-04-16T14:43:30Z",
            "printer_id": "bambu-h2d",
            "direction": "recv",
            "protocol": "mqtt",
            "topic": "device/xxx/report",
            "payload": {"print": {"gcode_state": "RUNNING", "mc_percent": 50}},
        }
        event = line_to_event(line, "test-01")
        assert isinstance(event, BambuReportEvent)
        assert event.printer_id == "test-01"
        assert event.section.gcode_state == "RUNNING"
        assert event.ts == 100.0

    def test_subscribe_is_connected(self):
        line = {
            "ts": 100.0,
            "printer_id": "bambu-h2d",
            "direction": "event",
            "protocol": "mqtt",
            "event": "subscribed",
        }
        event = line_to_event(line, "test-01")
        assert isinstance(event, ConnectionEvent)
        assert event.kind == "connected"

    def test_error_line_is_connection_error(self):
        line = {
            "ts": 100.0,
            "printer_id": "bambu-p1s",
            "direction": "error",
            "protocol": "mqtt",
            "error": "MqttError('Disconnected during message iteration')",
        }
        event = line_to_event(line, "test-01")
        assert isinstance(event, ConnectionEvent)
        assert event.kind == "error"
        assert "MqttError" in event.detail

    def test_unknown_line_returns_none(self):
        """Capture-envelope noise (heartbeats without payload) is skipped silently."""
        line = {"ts": 100.0, "direction": "recv", "event": "noise"}
        assert line_to_event(line, "test-01") is None

    def test_info_payload_is_info_event(self):
        from backend.modules.printers.telemetry.events import BambuInfoEvent
        line = {
            "ts": 100.0,
            "payload": {"info": {"command": "get_version", "module": []}},
        }
        event = line_to_event(line, "test-01")
        assert isinstance(event, BambuInfoEvent)


class TestIterEvents:
    def test_a1_kickoff_streams_events(self):
        fixture = FIXTURES / "bambu-a1-kickoff.jsonl"
        events = list(iter_events(fixture, printer_id="test-a1"))
        assert len(events) > 0
        assert all(e.printer_id == "test-a1" for e in events)

    def test_events_ts_monotonic(self):
        fixture = FIXTURES / "bambu-a1-kickoff.jsonl"
        events = list(iter_events(fixture, printer_id="test-a1"))
        timestamps = [e.ts for e in events]
        assert timestamps == sorted(timestamps)


class TestGapCompression:
    def test_large_gap_compressed(self, tmp_path):
        """Synthetic 2-event fixture with 1-hour gap → compressed to MAX_GAP_SEC."""
        fixture = tmp_path / "gap.jsonl"
        fixture.write_text(
            json.dumps({
                "ts": 1000.0, "iso": "2026-04-16T14:43:30Z",
                "payload": {"print": {"gcode_state": "IDLE"}},
            }) + "\n" +
            json.dumps({
                "ts": 4600.0, "iso": "2026-04-16T15:43:30Z",
                "payload": {"print": {"gcode_state": "PREPARE"}},
            }) + "\n"
        )
        events = list(iter_events(fixture, printer_id="t"))
        assert len(events) == 2
        # first event keeps its ts; second event compressed
        assert events[0].ts == 1000.0
        actual_gap = events[1].ts - events[0].ts
        assert actual_gap == MAX_GAP_SEC, (
            f"expected gap {MAX_GAP_SEC}, got {actual_gap}"
        )


class TestReplayEndToEnd:
    def test_a1_kickoff_matches_expected(self):
        """Snapshot test: replay fixture, compare transitions to expected.json."""
        fixture = FIXTURES / "bambu-a1-kickoff.jsonl"
        expected_path = FIXTURES / "bambu-a1-kickoff.expected.json"
        expected = json.loads(expected_path.read_text())

        result = replay(fixture, printer_id="bambu-a1")

        assert result.event_count == expected["event_count"]
        assert result.degraded_count == expected["degraded_count"]
        assert result.final_status.state.value == expected["final_state"]
        actual_transitions = [
            {
                "ts": t.ts,
                "from_state": t.from_state.value,
                "to_state": t.to_state.value,
                "reason": t.reason,
            }
            for t in result.transitions
        ]
        assert actual_transitions == expected["transitions"]

    def test_h2d_failure_matches_expected(self):
        """The headline test — h2d failure slice transitions PRINTING → ERROR
        via print_error overlay. Legacy adapter would have rendered this as
        a silent switch to IDLE. V2 surfaces the error state distinctly."""
        fixture = FIXTURES / "bambu-h2d-failure.jsonl"
        expected_path = FIXTURES / "bambu-h2d-failure.expected.json"
        expected = json.loads(expected_path.read_text())

        result = replay(fixture, printer_id="bambu-h2d")

        assert result.final_status.state.value == expected["final_state"]
        assert result.final_status.state == PrinterState.ERROR
        # at least one print_error ActiveError is present in final status
        assert any(e.source == "print_error" for e in result.final_status.active_errors)

        actual = [
            {
                "ts": t.ts,
                "from_state": t.from_state.value,
                "to_state": t.to_state.value,
                "reason": t.reason,
            }
            for t in result.transitions
        ]
        assert actual == expected["transitions"]

    def test_h2d_surfaces_hms_as_active_errors(self):
        """Adapter correctness: the h2d slice contains HMS entries that must
        become ActiveError rows. Legacy adapter dropped these on the floor."""
        fixture = FIXTURES / "bambu-h2d-failure.jsonl"
        result = replay(fixture, printer_id="bambu-h2d")
        hms_errors = [e for e in result.final_status.active_errors if e.source == "hms"]
        assert len(hms_errors) > 0, "no HMS active errors surfaced — adapter gap"


class TestReplayPurity:
    def test_replay_is_idempotent(self):
        """Two calls to replay() with the same input produce byte-identical output."""
        fixture = FIXTURES / "bambu-a1-kickoff.jsonl"
        r1 = replay(fixture, printer_id="bambu-a1")
        r2 = replay(fixture, printer_id="bambu-a1")
        assert r1.final_status == r2.final_status
        assert r1.transitions == r2.transitions


class TestSlicer:
    def test_slice_filters_by_iso(self, tmp_path):
        input = tmp_path / "in.jsonl"
        input.write_text(
            json.dumps({"iso": "2026-04-16T14:00:00Z", "ts": 1}) + "\n" +
            json.dumps({"iso": "2026-04-16T14:30:00Z", "ts": 2}) + "\n" +
            json.dumps({"iso": "2026-04-16T15:00:00Z", "ts": 3}) + "\n" +
            json.dumps({"iso": "2026-04-16T15:30:00Z", "ts": 4}) + "\n"
        )
        output = tmp_path / "out.jsonl"
        n = slice_capture(input, output, "2026-04-16T14:15:00", "2026-04-16T15:15:00")
        assert n == 2
        content = output.read_text()
        assert '"ts": 2' in content
        assert '"ts": 3' in content
        assert '"ts": 1' not in content
        assert '"ts": 4' not in content

    def test_reversed_range_raises(self, tmp_path):
        input = tmp_path / "in.jsonl"
        input.write_text("{}\n")
        with pytest.raises(ValueError, match="reversed"):
            slice_capture(input, tmp_path / "out.jsonl", "2026-04-16T14:00:00", "2026-04-16T13:00:00")

    def test_equal_range_raises(self, tmp_path):
        input = tmp_path / "in.jsonl"
        input.write_text("{}\n")
        with pytest.raises(ValueError, match="reversed or empty"):
            slice_capture(input, tmp_path / "out.jsonl", "T", "T")

    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            slice_capture(
                tmp_path / "nope.jsonl", tmp_path / "out.jsonl",
                "2026-04-16T14:00:00", "2026-04-16T15:00:00",
            )


class TestBootstrapExpectedStates:
    def test_bootstrap_round_trips(self):
        """Bootstrapping, then replaying, then bootstrapping again must converge."""
        fixture = FIXTURES / "bambu-a1-kickoff.jsonl"
        snapshot_1 = bootstrap_expected_states(fixture, printer_id="bambu-a1")
        snapshot_2 = bootstrap_expected_states(fixture, printer_id="bambu-a1")
        assert snapshot_1 == snapshot_2

    def test_bootstrap_has_all_required_fields(self):
        fixture = FIXTURES / "bambu-a1-kickoff.jsonl"
        snapshot = bootstrap_expected_states(fixture, printer_id="bambu-a1")
        assert "fixture" in snapshot
        assert "event_count" in snapshot
        assert "final_state" in snapshot
        assert "transitions" in snapshot
        assert isinstance(snapshot["transitions"], list)
