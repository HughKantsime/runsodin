"""Contract tests for BambuPrintSection (T1.5).

The `print` section is the heart of the Bambu MQTT report. 105 top-level
scalar fields observed across 4 Bambu printers; this module models ~45
first-class. Tests validate:

1. Key canonical fields parse correctly.
2. Polymorphic int/float temperatures normalize.
3. Polymorphic int/str stg_cur normalizes.
4. Full-corpus: every `print` payload across all 4 captures parses with
   zero errors.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection


# ---- Representative real payloads (trimmed to top-level scalars) ----

# Real bambu-h2d message mid-print with HMS active
H2D_PRINT_RUNNING = {
    "command": "push_status",
    "sequence_id": "12345",
    "msg": 0,
    "gcode_state": "RUNNING",
    "stg_cur": 2,
    "mc_print_stage": "2",
    "mc_print_sub_stage": 0,
    "mc_percent": 45,
    "layer_num": 27,
    "total_layer_num": 54,
    "mc_remaining_time": 65,
    "bed_temper": 60.0,
    "bed_target_temper": 60.0,
    "nozzle_temper": 220.5,
    "nozzle_target_temper": 220.0,
    "chamber_temper": 35,
    "cooling_fan_speed": "15",
    "heatbreak_fan_speed": "255",
    "gcode_file": "/sdcard/my_print.gcode",
    "subtask_id": "4821",
    "task_id": "99887766",
    "project_id": "1234",
    "subtask_name": "Dragon_T-Rex_v3",
    "spd_lvl": 2,
    "spd_mag": 100,
    "print_error": 0,
    "hms": [
        {"attr": 83886592, "code": 196618},
        {"attr": 201326848, "code": 131095},
    ],
    "wifi_signal": "-62dBm",
    "sdcard": True,
    "home_flag": 0,
    "state": 0,
}

# Real bambu-a1 mid-print (minimal — no chamber sensor, no cols)
A1_PRINT_RUNNING = {
    "command": "push_status",
    "sequence_id": "67890",
    "msg": 5,
    "gcode_state": "RUNNING",
    "stg_cur": 14,  # printing layer
    "mc_percent": 80,
    "layer_num": 192,
    "total_layer_num": 240,
    "mc_remaining_time": 12,
    "bed_temper": 60,    # int form!
    "bed_target_temper": 60,
    "nozzle_temper": 210,
    "nozzle_target_temper": 210,
    "cooling_fan_speed": "0",
    "gcode_file": "/sdcard/benchy.gcode",
    "spd_lvl": 2,
    "print_error": 0,
    "wifi_signal": "-55dBm",
}

# Temperature polymorphism — Bambu sometimes sends int, sometimes float
POLYMORPHIC_TEMP_INT = {"bed_temper": 60, "nozzle_temper": 210}
POLYMORPHIC_TEMP_FLOAT = {"bed_temper": 60.5, "nozzle_temper": 210.7}

# stg_cur polymorphism — observed as both int and str
POLYMORPHIC_STG_INT = {"stg_cur": 2}
POLYMORPHIC_STG_STR = {"stg_cur": "2"}


class TestCanonicalFields:
    def test_parse_h2d_running(self):
        p = BambuPrintSection.model_validate(H2D_PRINT_RUNNING)
        assert p.gcode_state == "RUNNING"
        assert p.stg_cur == 2
        assert p.mc_percent == 45
        assert p.layer_num == 27
        assert p.total_layer_num == 54
        assert p.bed_temper == 60.0
        assert p.nozzle_temper == 220.5
        assert p.chamber_temper == 35.0
        assert p.subtask_id == "4821"
        assert p.subtask_name == "Dragon_T-Rex_v3"
        assert p.print_error == 0
        assert len(p.hms) == 2
        assert p.hms[0].attr == 83886592

    def test_parse_a1_running(self):
        p = BambuPrintSection.model_validate(A1_PRINT_RUNNING)
        assert p.gcode_state == "RUNNING"
        assert p.stg_cur == 14
        # No chamber sensor on A1 — must be None, not 0
        assert p.chamber_temper is None

    def test_empty_print_accepts(self):
        """All fields Optional; empty dict must parse (edge case: setup messages)."""
        p = BambuPrintSection.model_validate({})
        assert p.gcode_state is None
        assert p.stg_cur is None
        assert p.hms == []


class TestTemperaturePolymorphism:
    def test_int_temps_coerce_to_float(self):
        p = BambuPrintSection.model_validate(POLYMORPHIC_TEMP_INT)
        assert p.bed_temper == 60.0
        assert isinstance(p.bed_temper, float)
        assert p.nozzle_temper == 210.0
        assert isinstance(p.nozzle_temper, float)

    def test_float_temps_pass_through(self):
        p = BambuPrintSection.model_validate(POLYMORPHIC_TEMP_FLOAT)
        assert p.bed_temper == 60.5
        assert p.nozzle_temper == 210.7

    def test_string_temp_coerces(self):
        """Some firmware sends stringified temps. Coerce or raise."""
        p = BambuPrintSection.model_validate({"bed_temper": "60.5"})
        assert p.bed_temper == 60.5

    def test_null_temps_stay_none(self):
        p = BambuPrintSection.model_validate({"bed_temper": None})
        assert p.bed_temper is None


class TestStgCurPolymorphism:
    def test_int_stg_cur(self):
        p = BambuPrintSection.model_validate(POLYMORPHIC_STG_INT)
        assert p.stg_cur == 2
        assert isinstance(p.stg_cur, int)

    def test_str_stg_cur_normalizes_to_int(self):
        """The bug from the spec — captures show both forms of same value."""
        p = BambuPrintSection.model_validate(POLYMORPHIC_STG_STR)
        assert p.stg_cur == 2
        assert isinstance(p.stg_cur, int)

    def test_negative_stg_cur(self):
        """-1 is a valid sentinel (failed state)."""
        p = BambuPrintSection.model_validate({"stg_cur": -1})
        assert p.stg_cur == -1
        p2 = BambuPrintSection.model_validate({"stg_cur": "-1"})
        assert p2.stg_cur == -1

    def test_unparseable_stg_cur_raises(self):
        """Fail loud on garbage — do not coerce to 0 or None silently."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BambuPrintSection.model_validate({"stg_cur": "not_a_number"})


class TestHMSEmbedding:
    def test_hms_list_parses(self):
        p = BambuPrintSection.model_validate({
            "hms": [
                {"attr": 1, "code": 2},
                {"attr": 3, "code": 4},
            ]
        })
        assert len(p.hms) == 2
        assert p.hms[0].key == "00000001_00000002"
        assert p.hms[1].key == "00000003_00000004"

    def test_empty_hms_list_is_empty(self):
        p = BambuPrintSection.model_validate({"hms": []})
        assert p.hms == []

    def test_missing_hms_defaults_to_empty(self):
        p = BambuPrintSection.model_validate({})
        assert p.hms == []


class TestExtraFields:
    def test_unknown_field_captured_not_rejected(self):
        p = BambuPrintSection.model_validate({
            "gcode_state": "RUNNING",
            "new_firmware_field": 42,
            "another_unknown": "xyz",
        })
        assert p.model_extra is not None
        assert "new_firmware_field" in p.model_extra
        assert "another_unknown" in p.model_extra


class TestAgainstFullCapture:
    """Parse 100% of print payloads across all Bambu captures."""

    @pytest.mark.parametrize("printer_file", [
        "bambu-a1.jsonl",
        "bambu-h2d.jsonl",
        "bambu-p1s.jsonl",
        "bambu-x1c.jsonl",
    ])
    def test_print_section_parses_every_captured_payload(self, printer_file):
        capture_dir = Path.home() / "Documents/Claude/odin-e2e/captures/run-2026-04-16"
        path = capture_dir / printer_file
        if not path.exists():
            pytest.skip(f"capture not available at {path}")

        seen = 0
        errors = []
        with path.open() as f:
            for i, line in enumerate(f):
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = e.get("payload") or e
                print_section = payload.get("print")
                if not isinstance(print_section, dict):
                    continue
                try:
                    BambuPrintSection.model_validate(print_section)
                    seen += 1
                except Exception as exc:
                    errors.append((i, str(exc)[:300]))
                    if len(errors) >= 5:
                        break

        assert not errors, (
            f"{printer_file}: {len(errors)} parse errors. "
            f"First 5: {errors[:5]}"
        )
        assert seen > 0, f"{printer_file}: no print payloads found"
