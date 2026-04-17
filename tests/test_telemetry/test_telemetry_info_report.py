"""Contract tests for BambuInfoSection + BambuInfoModule + BambuReport (T1.6 + T1.7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.bambu.raw import (
    BambuInfoModule,
    BambuInfoSection,
    BambuReport,
    InvalidBambuReport,
)


# Real `info` module records extracted from captures
A1_OTA_MODULE = {
    "name": "ota",
    "sw_ver": "01.07.00.00",
    "hw_ver": "OTA",
    "loader_ver": "00.00.00.00",
    "sn": "03919D4B3003552",
    "product_name": "Bambu Lab A1",
    "visible": True,
    "flag": 0,
}

P1S_OTA_MODULE_WITH_UPDATE = {
    "name": "ota",
    "sw_ver": "01.09.00.00",
    "hw_ver": "OTA",
    "loader_ver": "00.00.00.00",
    "sn": "01P09C552900533",
    "product_name": "Bambu Lab P1S",
    "visible": True,
    "new_ver": "01.09.01.00",
    "flag": 15,
}

H2D_MINIMAL_MODULE = {
    "name": "mc",
    "sw_ver": "01.03.00.00",
    "flag": 3,
    "hw_ver": "N/A",
    "loader_ver": "00.00.00.00",
}

X1C_INFO_SECTION = {
    "command": "get_version",
    "sequence_id": "0",
    "module": [
        {
            "flag": 3, "hw_ver": "N/A", "loader_ver": "00.00.00.00",
            "name": "ota", "product_name": "Bambu Lab X1-Carbon",
            "sn": "00M09D4B1600284", "sw_ver": "01.11.02.00", "visible": True,
        },
    ],
}


class TestBambuInfoModule:
    def test_parse_a1_ota(self):
        m = BambuInfoModule.model_validate(A1_OTA_MODULE)
        assert m.name == "ota"
        assert m.sw_ver == "01.07.00.00"
        assert m.product_name == "Bambu Lab A1"
        assert m.sn == "03919D4B3003552"
        assert m.new_ver is None

    def test_parse_p1s_module_with_update(self):
        m = BambuInfoModule.model_validate(P1S_OTA_MODULE_WITH_UPDATE)
        assert m.new_ver == "01.09.01.00"
        assert m.sw_ver == "01.09.00.00"

    def test_parse_minimal_module(self):
        """H2D non-ota modules (mc, esp32) have no product_name/sn."""
        m = BambuInfoModule.model_validate(H2D_MINIMAL_MODULE)
        assert m.name == "mc"
        assert m.product_name is None
        assert m.sn is None
        assert m.visible is None

    def test_name_required(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BambuInfoModule.model_validate({"sw_ver": "0"})

    def test_sw_ver_required(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            BambuInfoModule.model_validate({"name": "ota"})


class TestBambuInfoSection:
    def test_parse_x1c_info(self):
        s = BambuInfoSection.model_validate(X1C_INFO_SECTION)
        assert s.command == "get_version"
        assert len(s.module) == 1
        assert s.module[0].product_name == "Bambu Lab X1-Carbon"

    def test_info_with_a1_ack(self):
        s = BambuInfoSection.model_validate({
            "command": "get_version",
            "sequence_id": "1",
            "module": [],
            "reason": "success",
            "result": "success",
        })
        assert s.reason == "success"
        assert s.result == "success"


class TestBambuReport:
    def test_report_with_print_only(self):
        r = BambuReport.model_validate({
            "print": {"gcode_state": "RUNNING", "mc_percent": 50}
        })
        assert r.print is not None
        assert r.info is None
        assert r.print.gcode_state == "RUNNING"

    def test_report_with_info_only(self):
        r = BambuReport.model_validate({"info": X1C_INFO_SECTION})
        assert r.info is not None
        assert r.print is None

    def test_report_with_both(self):
        r = BambuReport.model_validate({
            "print": {"gcode_state": "IDLE"},
            "info": X1C_INFO_SECTION,
        })
        assert r.print is not None
        assert r.info is not None

    def test_report_with_neither_fails_loud(self):
        """Fail-loud: unknown envelope shape raises.

        Pydantic wraps raises-from-model_post_init in ValidationError, so
        we check for the specific message. The underlying InvalidBambuReport
        context is preserved in the error body.
        """
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="neither"):
            BambuReport.model_validate({"foo": "bar"})

    def test_report_empty_fails_loud(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="neither"):
            BambuReport.model_validate({})


class TestReportAgainstFullCapture:
    """Every captured line with a recognized payload must parse as a BambuReport."""

    @pytest.mark.parametrize("printer_file", [
        "bambu-a1.jsonl",
        "bambu-h2d.jsonl",
        "bambu-p1s.jsonl",
        "bambu-x1c.jsonl",
    ])
    def test_report_parses_every_print_or_info_line(self, printer_file):
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
                payload = e.get("payload")
                if not isinstance(payload, dict):
                    continue
                # Only attempt lines that actually carry print or info.
                # Subscribe/error/heartbeat envelopes legitimately have neither
                # and would correctly raise InvalidBambuReport — those are
                # handled in the adapter layer, not at model-validation level.
                if "print" not in payload and "info" not in payload:
                    continue
                try:
                    BambuReport.model_validate(payload)
                    seen += 1
                except Exception as exc:
                    errors.append((i, str(exc)[:300]))
                    if len(errors) >= 5:
                        break

        assert not errors, (
            f"{printer_file}: {len(errors)} parse errors. First 5: {errors[:5]}"
        )
        assert seen > 0, f"{printer_file}: no report payloads parsed"
