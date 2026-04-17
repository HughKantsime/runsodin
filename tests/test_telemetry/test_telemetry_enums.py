"""Contract tests for BambuGcodeState enum (T1.8).

The enum replaces the legacy adapter's silent string comparison. Unknown
values MUST raise, not fall through to an `UNKNOWN` fallback.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.modules.printers.telemetry.bambu.enums import BambuGcodeState
from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection


class TestBambuGcodeState:
    def test_all_observed_members(self):
        """Every value observed in run-2026-04-16 captures must be a member."""
        expected = {"IDLE", "PREPARE", "RUNNING", "PAUSE", "FAILED", "FINISH"}
        actual = {m.value for m in BambuGcodeState}
        assert actual == expected

    def test_str_subclass_for_backward_compat(self):
        """`BambuGcodeState` is a str Enum — existing code comparing
        state == "RUNNING" keeps working during the rewrite."""
        assert BambuGcodeState.RUNNING == "RUNNING"
        assert BambuGcodeState.FAILED == "FAILED"

    def test_parse_from_string(self):
        assert BambuGcodeState("RUNNING") == BambuGcodeState.RUNNING

    def test_reject_unknown_value(self):
        with pytest.raises(ValueError):
            BambuGcodeState("UNKNOWN_STATE")

    def test_case_sensitive(self):
        """Bambu firmware consistently sends uppercase. Lowercase is unknown."""
        with pytest.raises(ValueError):
            BambuGcodeState("running")


class TestPrintSectionStrictGcodeState:
    def test_accepts_valid_state(self):
        for state in ("IDLE", "PREPARE", "RUNNING", "PAUSE", "FAILED", "FINISH"):
            p = BambuPrintSection.model_validate({"gcode_state": state})
            assert p.gcode_state == state
            assert isinstance(p.gcode_state, BambuGcodeState)

    def test_rejects_unknown_state(self):
        """The key fail-loud moment. Legacy adapter silently mapped this to UNKNOWN."""
        with pytest.raises(ValidationError):
            BambuPrintSection.model_validate({"gcode_state": "CRASHED_HARD"})

    def test_rejects_empty_string(self):
        with pytest.raises(ValidationError):
            BambuPrintSection.model_validate({"gcode_state": ""})

    def test_none_still_valid(self):
        """Field is Optional — None = no state reported in this message."""
        p = BambuPrintSection.model_validate({"gcode_state": None})
        assert p.gcode_state is None

    def test_missing_still_valid(self):
        """Field absent = no state in this message; still a valid print section."""
        p = BambuPrintSection.model_validate({"mc_percent": 50})
        assert p.gcode_state is None
