"""Contract tests for Bambu AMS models (T1.4).

Tests parse REAL AMS payloads extracted from
odin-e2e/captures/run-2026-04-16 across all 4 Bambu models. The
fixtures are stored as constants below (representative, not
exhaustive); full-corpus validation lives in
test_ams_against_full_capture.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.bambu.raw import (
    BambuAMSRoot,
    BambuAMSTray,
    BambuAMSUnit,
)

# ---------- Representative real payloads ----------

# bambu-a1 — fully-populated tray (PLA Basic black, full roll)
A1_TRAY = {
    "id": "0",
    "state": 3,
    "remain": 0,
    "k": 0.019999999552965164,
    "n": 1,
    "cali_idx": -1,
    "total_len": 330000,
    "tag_uid": "7D1B7F3600000100",
    "tray_id_name": "A00-K00",
    "tray_info_idx": "GFA00",
    "tray_type": "PLA",
    "tray_sub_brands": "PLA Basic",
    "tray_color": "000000FF",
    "tray_weight": "1000",
    "tray_diameter": "1.75",
    "tray_temp": "55",
    "tray_time": "8",
    "bed_temp_type": "0",
    "bed_temp": "0",
    "nozzle_temp_max": "230",
    "nozzle_temp_min": "190",
    "xcam_info": "803E803EE803E803CDCC4C3F",
    "tray_uuid": "CD723B683D244E6F95885E1F45D43967",
    "ctype": 0,
}

# bambu-h2d — fully-populated tray with drying fields + cols
H2D_TRAY = {
    "bed_temp": "0",
    "bed_temp_type": "0",
    "cali_idx": -1,
    "cols": ["FFFFFFFF"],
    "ctype": 2,
    "drying_temp": "55",
    "drying_time": "8",
    "id": "0",
    "nozzle_temp_max": "230",
    "nozzle_temp_min": "190",
    "remain": 70,
    "state": 11,
    "tag_uid": "CD06F72E00000100",
    "total_len": 330000,
    "tray_color": "FFFFFFFF",
    "tray_diameter": "1.75",
    "tray_id_name": "A00-W01",
    "tray_info_idx": "GFA00",
    "tray_sub_brands": "PLA Basic",
    "tray_type": "PLA",
    "tray_uuid": "B4A62373E04B498B80254BCA9E5A9748",
    "tray_weight": "1000",
    "xcam_info": "000000000000000000000000",
}

# bambu-p1s — MINIMAL tray (only id present). 3,164 such messages captured.
P1S_MINIMAL_TRAY = {"id": "0"}

# bambu-x1c — tray with remain=-1 (unknown) and empty identity
X1C_EMPTY_SLOT = {
    "bed_temp": "0",
    "bed_temp_type": "0",
    "cali_idx": -1,
    "cols": ["FFFFFFFF"],
    "ctype": 0,
    "drying_temp": "0",
    "drying_time": "0",
    "id": "0",
    "nozzle_temp_max": "270",
    "nozzle_temp_min": "230",
    "remain": -1,
    "state": 11,
    "tag_uid": "0000000000000000",
    "total_len": 330000,
    "tray_color": "FFFFFFFF",
    "tray_diameter": "1.75",
    "tray_id_name": "",
    "tray_info_idx": "GFG02",
    "tray_sub_brands": "",
    "tray_type": "PETG",
    "tray_uuid": "00000000000000000000000000000000",
    "tray_weight": "0",
    "xcam_info": "000000000000000000000000",
}


class TestBambuAMSTray:
    def test_parse_a1_full_tray(self):
        tray = BambuAMSTray.model_validate(A1_TRAY)
        assert tray.id == "0"
        assert tray.tray_type == "PLA"
        assert tray.tray_sub_brands == "PLA Basic"
        assert tray.tray_color == "000000FF"
        assert tray.remain == 0
        assert tray.total_len == 330000
        assert tray.k == pytest.approx(0.02)
        assert tray.ctype == 0

    def test_parse_h2d_full_tray(self):
        tray = BambuAMSTray.model_validate(H2D_TRAY)
        assert tray.id == "0"
        assert tray.tray_type == "PLA"
        assert tray.remain == 70
        assert tray.state == 11
        assert tray.cols == ["FFFFFFFF"]
        assert tray.drying_temp == "55"
        assert tray.drying_time == "8"

    def test_parse_p1s_minimal_tray(self):
        """P1S sends trays with only {id} — all other fields must be Optional."""
        tray = BambuAMSTray.model_validate(P1S_MINIMAL_TRAY)
        assert tray.id == "0"
        assert tray.tray_type is None
        assert tray.tray_color is None
        assert tray.remain is None
        assert tray.total_len is None

    def test_parse_x1c_empty_slot(self):
        """X1C empty-slot tray: remain=-1 (sentinel for unknown/empty), blank identity."""
        tray = BambuAMSTray.model_validate(X1C_EMPTY_SLOT)
        assert tray.id == "0"
        assert tray.remain == -1
        assert tray.tray_id_name == ""
        assert tray.tray_sub_brands == ""
        assert tray.tray_type == "PETG"
        assert tray.tag_uid == "0000000000000000"

    def test_id_is_required(self):
        """`id` is the only required field — reject a tray with no id."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BambuAMSTray.model_validate({"tray_type": "PLA"})

    def test_extra_fields_captured_not_rejected(self):
        """Future Bambu firmware may add fields; extra=allow keeps them (for the observer)."""
        payload = {**A1_TRAY, "new_firmware_field": "some_value"}
        tray = BambuAMSTray.model_validate(payload)
        assert tray.model_extra is not None
        assert "new_firmware_field" in tray.model_extra


class TestBambuAMSUnit:
    def test_parse_h2d_unit_with_dry_setting(self):
        raw = {
            "id": "0",
            "humidity": "25",
            "humidity_raw": "23.1",
            "temp": "30",
            "dry_time": 0,
            "dry_sf_reason": [],
            "dry_setting": {
                "dry_duration": 0,
                "dry_filament": "",
                "dry_temperature": 55,
            },
            "tray": [H2D_TRAY, H2D_TRAY, H2D_TRAY, H2D_TRAY],
        }
        unit = BambuAMSUnit.model_validate(raw)
        assert unit.id == "0"
        assert unit.humidity == "25"
        assert unit.dry_setting is not None
        assert unit.dry_setting.dry_temperature == 55
        assert len(unit.tray) == 4

    def test_parse_p1s_unit_minimal_trays(self):
        raw = {
            "ams_id": "0",
            "check": 1,
            "chip_id": "abc123",
            "humidity": "50",
            "humidity_raw": "48.2",
            "id": "0",
            "info": "",
            "temp": "25",
            "dry_time": 0,
            "tray": [{"id": "0"}, {"id": "1"}, {"id": "2"}, {"id": "3"}],
        }
        unit = BambuAMSUnit.model_validate(raw)
        assert unit.ams_id == "0"
        assert unit.check == 1
        assert len(unit.tray) == 4
        assert unit.tray[0].id == "0"

    def test_parse_x1c_unit_no_dry_setting(self):
        raw = {
            "id": "0",
            "humidity": "30",
            "humidity_raw": "28.5",
            "temp": "27",
            "dry_time": 0,
            "info": "",
            "tray": [X1C_EMPTY_SLOT],
        }
        unit = BambuAMSUnit.model_validate(raw)
        assert unit.id == "0"
        assert unit.dry_setting is None
        assert unit.temp == "27"


class TestBambuAMSRoot:
    def test_parse_p1s_minimal_root(self):
        """P1S sends only {ams: [...]} — all root-level flags absent."""
        raw = {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}
        root = BambuAMSRoot.model_validate(raw)
        assert len(root.ams) == 1
        assert root.tray_now is None
        assert root.ams_exist_bits is None

    def test_parse_h2d_rich_root(self):
        raw = {
            "ams": [],
            "ams_exist_bits": "1",
            "ams_exist_bits_raw": "1",
            "cali_id": 0,
            "cali_stat": 0,
            "insert_flag": False,
            "power_on_flag": True,
            "tray_exist_bits": "f",
            "tray_hall_out_bits": "0",
            "tray_is_bbl_bits": "f",
            "tray_now": "255",
            "tray_pre": "255",
            "tray_read_done_bits": "f",
            "tray_reading_bits": "0",
            "tray_tar": "255",
            "unbind_ams_stat": 0,
            "version": 17,
        }
        root = BambuAMSRoot.model_validate(raw)
        assert root.tray_now == "255"
        assert root.tray_exist_bits == "f"
        assert root.power_on_flag is True
        assert root.version == 17


class TestAMSAgainstFullCapture:
    """Parse 100% of `print.ams` payloads across all Bambu captures without error."""

    @pytest.mark.parametrize("printer_file", [
        "bambu-a1.jsonl",
        "bambu-h2d.jsonl",
        "bambu-p1s.jsonl",
        "bambu-x1c.jsonl",
    ])
    def test_ams_parses_every_captured_payload(self, printer_file):
        """Walk the capture file, parse every `print.ams` object, assert zero failures.

        This is the acceptance test for T1.4: the AMS models must accept
        100% of real Bambu payloads. Any parse failure is a model bug.
        """
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
                ams = (payload.get("print") or {}).get("ams")
                if not isinstance(ams, dict):
                    continue
                try:
                    BambuAMSRoot.model_validate(ams)
                    seen += 1
                except Exception as exc:
                    errors.append((i, str(exc)[:200]))
                    if len(errors) >= 3:
                        break  # report first 3 failures

        assert not errors, (
            f"{printer_file}: {len(errors)} parse errors in AMS models. "
            f"First 3: {errors[:3]}"
        )
        # Every Bambu capture has at least some ams payloads.
        assert seen > 0, f"{printer_file}: no ams payloads found — capture or test bug"
