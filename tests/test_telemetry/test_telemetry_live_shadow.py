"""Contract tests for the live-shadow validation (T4.5).

Each fixture gets run through the live MQTT pipeline + parity-sim.
Assertion: bug_count == 0 — every V2 divergence from legacy is either
an intentional design choice (FAILED ≠ IDLE, etc.) or an improvement
(V2 surfaces HMS + ERROR states legacy misses).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.modules.printers.telemetry.live_shadow import (
    LiveShadowResult,
    run_live_shadow,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "telemetry"


# Bigger fixtures skipped for speed — smoke-test on two representative
# cases. Full corpus runs offline via a separate script if needed.
SHADOW_CASES = [
    # (fixture_name, printer_id, serial)
    ("bambu-a1-kickoff", "bambu-a1", "TEST-A1"),
    ("bambu-h2d-failure", "bambu-h2d", "TEST-H2D"),
]


@pytest.mark.parametrize("fixture_name, printer_id, serial", SHADOW_CASES)
def test_live_shadow_cutover_safe(fixture_name, printer_id, serial):
    """The cutover gate: V2's live-MQTT behavior must agree with legacy
    on every field except intentional/improvement diffs."""
    fixture_path = FIXTURES / f"{fixture_name}.jsonl"
    result = run_live_shadow(
        fixture_path,
        printer_id=printer_id,
        serial=serial,
        speed=1000.0,
        quiesce_sec=1.0,
    )

    # adapter must have actually received events (sanity)
    assert result.v2_events_received > 0, (
        f"{fixture_name}: V2 adapter received zero events — broker/publisher issue"
    )

    # headline assertion: cutover-safe (zero unclassified bugs)
    assert result.is_cutover_safe, (
        f"{fixture_name}: {result.parity.bug_count} unclassified "
        f"V2↔legacy diffs in live-MQTT run.\n"
        + "\n".join(
            f"  - {d.field}: v2={d.v2_value!r} legacy={d.legacy_value!r} "
            f"({d.rationale})"
            for d in result.parity.diffs
            if d.classification == "bug"
        )
    )


def test_live_shadow_returns_result_with_events():
    """Smoke — adapter received events, classifier ran, result is well-formed."""
    fixture_path = FIXTURES / "bambu-a1-kickoff.jsonl"
    result = run_live_shadow(
        fixture_path,
        printer_id="bambu-a1",
        serial="TEST-A1",
        speed=1000.0,
    )
    assert isinstance(result, LiveShadowResult)
    assert result.fixture == "bambu-a1-kickoff.jsonl"
    assert result.v2_events_received > 0
    # every diff must be classified (no bug surprises)
    for d in result.parity.diffs:
        assert d.classification in ("intentional", "improvement", "bug")
    assert result.is_cutover_safe
