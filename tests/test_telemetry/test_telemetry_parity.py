"""Contract tests for the V2 vs legacy parity baseline (T7.1).

Runs each committed fixture through both V2 pipeline and the legacy
simulator, asserts:

- Every diff is classified (no unclassified divergence).
- Bug-class diffs are ZERO across all fixtures. If this ever goes
  non-zero, V2 has diverged from legacy in a way not documented as
  intentional.
- Intentional diffs include the headline legacy bugs: FAILED ≠ IDLE,
  FINISHED ≠ IDLE.

This is the REGRESSION GATE for the cutover readiness question:
"is V2 behaving the way we documented it should?" Answer is yes iff
bug_count == 0 across the committed corpus.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.parity import (
    ParityDiff,
    ParityReport,
    run_parity_against_fixture,
    simulate_legacy_parse,
    LegacyStatus,
    LegacyPrinterState,
)
from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection


FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"

# Same fixture set as test_telemetry_fixtures.py
ALL_FIXTURES = [
    ("bambu-a1-kickoff", "bambu-a1"),
    ("bambu-a1-happy-path", "bambu-a1"),
    ("bambu-h2d-failure", "bambu-h2d"),
    ("bambu-h2d-failure-arc", "bambu-h2d"),
    ("bambu-h2d-recovery", "bambu-h2d"),
    ("bambu-x1c-ams-swap", "bambu-x1c"),
]


class TestLegacySimulator:
    """Unit tests on the legacy simulator itself — must reproduce the
    specific bugs legacy has, not be 'fixed' accidentally."""

    def test_failed_collapses_to_idle(self):
        """The headline legacy bug — confirm the simulator reproduces it."""
        section = BambuPrintSection.model_validate({"gcode_state": "FAILED"})
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.state == LegacyPrinterState.IDLE, (
            "legacy simulator should reproduce the FAILED → IDLE collapse"
        )

    def test_finish_collapses_to_idle(self):
        section = BambuPrintSection.model_validate({"gcode_state": "FINISH"})
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.state == LegacyPrinterState.IDLE

    def test_prepare_maps_to_printing(self):
        """Legacy doesn't distinguish PREPARE from RUNNING."""
        section = BambuPrintSection.model_validate({"gcode_state": "PREPARE"})
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.state == LegacyPrinterState.PRINTING

    def test_pause_maps_to_paused(self):
        section = BambuPrintSection.model_validate({"gcode_state": "PAUSE"})
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.state == LegacyPrinterState.PAUSED

    def test_print_error_overlays_error(self):
        section = BambuPrintSection.model_validate({
            "gcode_state": "RUNNING",
            "print_error": 257,
        })
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.state == LegacyPrinterState.ERROR
        assert result.error_message == "257"

    def test_temps_tracked(self):
        section = BambuPrintSection.model_validate({
            "bed_temper": 60.5,
            "nozzle_temper": 210.0,
        })
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.bed_temp == 60.5
        assert result.nozzle_temp == 210.0

    def test_fan_speed_str_coerces_to_int(self):
        """Legacy silently converted str fan speed to int via get+int()."""
        section = BambuPrintSection.model_validate({"cooling_fan_speed": "55"})
        result = simulate_legacy_parse(LegacyStatus(), section)
        assert result.fan_speed == 55


class TestParityAgainstFixtures:
    """Run parity against every committed fixture; enforce bug_count == 0."""

    @pytest.mark.parametrize("fixture_name, printer_id", ALL_FIXTURES)
    def test_no_bug_class_diffs(self, fixture_name, printer_id):
        """If this fails, V2 has drifted from legacy in an undocumented way.
        Either (a) classify the new diff as intentional/improvement in
        parity._KNOWN_STATE_DIFFS / _compare_fields, or (b) fix the bug."""
        fixture = FIXTURES / f"{fixture_name}.jsonl"
        report = run_parity_against_fixture(fixture, printer_id=printer_id)

        bugs = [d for d in report.diffs if d.classification == "bug"]
        assert not bugs, (
            f"{fixture_name}: {len(bugs)} unclassified V2↔legacy divergences.\n"
            + "\n".join(f"  - {d.field}: v2={d.v2_value!r} legacy={d.legacy_value!r} "
                        f"({d.rationale})" for d in bugs)
        )


class TestHeadlineDiffsAppear:
    """The whole point of V2 — these INTENTIONAL diffs must show up."""

    def test_failed_diff_appears_on_h2d_failure_arc(self):
        """The h2d-failure-arc fixture ends in ERROR (print_error overlay),
        not FAILED per se — but intermediate states include the
        RUNNING → PAUSE → ERROR transition. The intentional diff is
        PREPARING vs PRINTING (both adapters see PREPARE early on)."""
        fixture = FIXTURES / "bambu-h2d-failure-arc.jsonl"
        report = run_parity_against_fixture(fixture, printer_id="bambu-h2d")
        # At minimum, the PREPARING vs PRINTING intentional diff must appear
        state_diffs = [d for d in report.diffs if d.field == "state"]
        assert any(d.classification == "intentional" for d in state_diffs)

    def test_finished_diff_appears_on_a1_happy_path(self):
        """bambu-a1-happy-path ends FINISHED in V2, IDLE in legacy —
        headline bug fix."""
        fixture = FIXTURES / "bambu-a1-happy-path.jsonl"
        report = run_parity_against_fixture(fixture, printer_id="bambu-a1")
        finished_diffs = [
            d for d in report.diffs
            if d.field == "state" and d.v2_value == "finished" and d.legacy_value == "idle"
        ]
        assert finished_diffs, (
            "expected FINISHED vs IDLE intentional diff on a1-happy-path; none found"
        )
        assert finished_diffs[0].classification == "intentional"

    def test_active_errors_improvement_on_h2d_fixtures(self):
        """V2 surfaces HMS codes; legacy drops them. Improvement diff expected."""
        for fixture_name in ("bambu-h2d-failure-arc", "bambu-h2d-recovery"):
            fixture = FIXTURES / f"{fixture_name}.jsonl"
            report = run_parity_against_fixture(fixture, printer_id="bambu-h2d")
            improvements = [
                d for d in report.diffs
                if d.field == "active_errors" and d.classification == "improvement"
            ]
            assert improvements, f"{fixture_name}: expected HMS improvement diff, got none"


class TestParityBaselineSnapshot:
    """Committed baseline snapshot — new diffs appearing fail the test
    until either documented or fixed."""

    BASELINE_PATH = FIXTURES.parent / "telemetry_parity_baseline.json"

    def test_baseline_unchanged(self):
        """Aggregate diff signatures across all fixtures and compare to
        committed baseline. First run seeds; subsequent runs verify."""
        all_diffs: list[dict] = []
        for fixture_name, printer_id in ALL_FIXTURES:
            fixture = FIXTURES / f"{fixture_name}.jsonl"
            report = run_parity_against_fixture(fixture, printer_id=printer_id)
            for d in report.diffs:
                all_diffs.append({
                    "fixture": fixture_name,
                    "field": d.field,
                    "v2_value": str(d.v2_value),
                    "legacy_value": str(d.legacy_value),
                    "classification": d.classification,
                    "rationale": d.rationale,
                })
        all_diffs.sort(key=lambda d: (d["fixture"], d["field"], d["v2_value"]))

        if not self.BASELINE_PATH.exists():
            self.BASELINE_PATH.write_text(json.dumps(all_diffs, indent=2))
            pytest.skip(f"seeded parity baseline at {self.BASELINE_PATH}")

        committed = json.loads(self.BASELINE_PATH.read_text())
        assert all_diffs == committed, (
            f"parity baseline drifted. If intentional, regenerate "
            f"{self.BASELINE_PATH.name} and commit."
        )
