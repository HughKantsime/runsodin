"""Phase 5 contract tests — fixture-based snapshot gates (SPEC §6.2).

Parametrized across all committed fixtures. If any replay output drifts
from the checked-in expected.json, these tests fail — the drift must
either be a bug to fix or an explicit acknowledgment (regenerate the
snapshot deliberately and commit).

Additional assertions beyond pure snapshot matching:
- HMS codes surface as ActiveError on PrinterStatus.
- No parse failures: degraded_count is 0 across the committed corpus.
- Replay is deterministic (idempotent).
- Known-unmapped-field allowlist doesn't regress.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.observability import observer
from backend.modules.printers.telemetry.replay import replay
from backend.modules.printers.telemetry.state import PrinterState


FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"

ALL_FIXTURES = [
    ("bambu-a1-kickoff", "bambu-a1"),
    ("bambu-a1-happy-path", "bambu-a1"),
    ("bambu-h2d-failure", "bambu-h2d"),
    ("bambu-h2d-failure-arc", "bambu-h2d"),
    ("bambu-h2d-recovery", "bambu-h2d"),
]


def _transitions_as_json(result):
    return [
        {
            "ts": t.ts,
            "from_state": t.from_state.value,
            "to_state": t.to_state.value,
            "reason": t.reason,
        }
        for t in result.transitions
    ]


# ---------- Snapshot tests ----------

class TestStateTimelineSnapshot:
    """Each fixture must replay to match its committed expected.json."""

    @pytest.mark.parametrize("fixture_name, printer_id", ALL_FIXTURES)
    def test_matches_expected(self, fixture_name, printer_id):
        fixture_path = FIXTURES / f"{fixture_name}.jsonl"
        expected_path = FIXTURES / f"{fixture_name}.expected.json"
        assert fixture_path.exists(), f"fixture missing: {fixture_path}"
        assert expected_path.exists(), f"snapshot missing: {expected_path}"

        expected = json.loads(expected_path.read_text())
        result = replay(fixture_path, printer_id=printer_id)

        assert result.event_count == expected["event_count"], (
            f"{fixture_name}: event_count drift "
            f"{expected['event_count']} → {result.event_count}"
        )
        assert result.degraded_count == expected["degraded_count"]
        assert result.final_status.state.value == expected["final_state"]
        actual = _transitions_as_json(result)
        assert actual == expected["transitions"], (
            f"{fixture_name}: transition timeline drifted. "
            f"Expected {len(expected['transitions'])} transitions, "
            f"got {len(actual)}."
        )


# ---------- Determinism ----------

class TestReplayIsDeterministic:
    @pytest.mark.parametrize("fixture_name, printer_id", ALL_FIXTURES)
    def test_idempotent(self, fixture_name, printer_id):
        fixture_path = FIXTURES / f"{fixture_name}.jsonl"
        r1 = replay(fixture_path, printer_id=printer_id)
        r2 = replay(fixture_path, printer_id=printer_id)
        assert r1.final_status == r2.final_status
        assert r1.transitions == r2.transitions
        assert r1.event_count == r2.event_count


# ---------- No parse failures on committed corpus ----------

class TestNoParseFailuresAcrossCorpus:
    @pytest.mark.parametrize("fixture_name, printer_id", ALL_FIXTURES)
    def test_zero_degraded_events(self, fixture_name, printer_id):
        """Committed fixtures must be fully modeled by the V2 pipeline.

        A non-zero degraded_count means either (a) a new Bambu firmware
        shape snuck in during slicing or (b) the model lost ground.
        Either way, visible — this test fails to force investigation.
        """
        fixture_path = FIXTURES / f"{fixture_name}.jsonl"
        result = replay(fixture_path, printer_id=printer_id)
        assert result.degraded_count == 0, (
            f"{fixture_name}: {result.degraded_count} degraded events. "
            f"Inspect the fixture and extend the model, or regenerate "
            f"the expected.json if this is an intentional loosening."
        )


# ---------- HMS surfacing ----------

class TestHMSSurfacesAsActiveError:
    def test_h2d_failure_surfaces_hms(self):
        """bambu-h2d captured with 4 HMS codes continuously active. V2
        surfaces each as an ActiveError (legacy dropped them silently)."""
        result = replay(
            FIXTURES / "bambu-h2d-failure-arc.jsonl",
            printer_id="bambu-h2d",
        )
        hms_errors = [e for e in result.final_status.active_errors if e.source == "hms"]
        assert len(hms_errors) >= 1, (
            "bambu-h2d-failure-arc should have surfaced at least one HMS code; "
            "got zero. Check that BambuHMSEvent flows through transition()."
        )

    def test_h2d_recovery_carries_hms_across_recovery(self):
        """Recovery slice starts with active HMS errors. They persist until
        explicitly cleared — verify they're in the final status."""
        result = replay(
            FIXTURES / "bambu-h2d-recovery.jsonl",
            printer_id="bambu-h2d",
        )
        hms_errors = [e for e in result.final_status.active_errors if e.source == "hms"]
        assert len(hms_errors) >= 1


# ---------- Headline legacy-bug regression checks ----------

class TestFailedDistinctFromIdle:
    """The headline V2 fix — FAILED must be a visible final state, not IDLE."""

    def test_h2d_failure_arc_ends_not_in_idle(self):
        result = replay(
            FIXTURES / "bambu-h2d-failure-arc.jsonl",
            printer_id="bambu-h2d",
        )
        assert result.final_status.state != PrinterState.IDLE
        # this fixture captures the pre-failure approach — ends on ERROR (print_error active)
        assert result.final_status.state in (
            PrinterState.FAILED, PrinterState.ERROR, PrinterState.PAUSED,
        )


class TestFinishedDistinctFromIdle:
    def test_a1_happy_path_ends_finished_not_idle(self):
        """bambu-a1 completed a full print in the captured window. The
        final state must be FINISHED — legacy collapsed FINISH to IDLE."""
        result = replay(
            FIXTURES / "bambu-a1-happy-path.jsonl",
            printer_id="bambu-a1",
        )
        assert result.final_status.state == PrinterState.FINISHED
        assert result.final_status.state != PrinterState.IDLE


# ---------- Allowlist regression for unmapped fields ----------

ALLOWLIST_PATH = FIXTURES.parent / "telemetry_unmapped_allowlist.json"


class TestUnmappedFieldAllowlist:
    """Replay each fixture, collect unmapped-field paths, assert the set is
    a subset of the committed allowlist.

    A new unmodeled field means either (a) a model gap that should be
    filled, or (b) a legitimate drift that needs the allowlist updated
    AFTER deliberate review. Either requires action — test fails
    until one happens.
    """

    def test_allowlist_regression(self):
        observer.reset()  # start clean
        for fixture_name, printer_id in ALL_FIXTURES:
            replay(FIXTURES / f"{fixture_name}.jsonl", printer_id=printer_id)

        snap = observer.snapshot()
        seen_paths = {r.field_path for r in snap if r.vendor == "bambu"}

        if not ALLOWLIST_PATH.exists():
            # First run — seed the allowlist. Subsequent runs verify.
            ALLOWLIST_PATH.write_text(
                json.dumps(sorted(seen_paths), indent=2)
            )
            pytest.skip(f"Seeded allowlist at {ALLOWLIST_PATH}; re-run to verify.")

        allowed = set(json.loads(ALLOWLIST_PATH.read_text()))
        new_paths = seen_paths - allowed

        assert not new_paths, (
            f"New unmapped Bambu fields detected (not on allowlist): "
            f"{sorted(new_paths)}. Either (a) model them in raw.py, or "
            f"(b) update {ALLOWLIST_PATH.name} deliberately."
        )
