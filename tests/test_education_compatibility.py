"""Golden tests for the Education compatibility engine."""

import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from modules.printers.education_compatibility import (  # noqa: E402
    ENGINE_VERSION,
    evaluate_education_compatibility,
)


def _fact(value, key):
    return {
        "present": value is not None and value != [] and value != "",
        "recognized": True,
        "source_member": "Metadata/project_settings.config",
        "source_key": key,
        "value": value,
    }


def _file(**overrides):
    facts = {
        "api_types": _fact(["bambu"], "printer_model"),
        "machine": _fact("Bambu Lab X1 Carbon", "printer_model"),
        "bed": _fact({"x_mm": 256, "y_mm": 256}, "printer_model"),
        "nozzle": _fact(0.4, "nozzle_diameter"),
        "materials": _fact(["PLA"], "filament_type"),
    }
    facts.update(overrides)
    return facts


def _printer(**overrides):
    facts = {
        "api_type": "bambu",
        "machine_type": "X1C",
        "bed_x_mm": 256,
        "bed_y_mm": 256,
        "nozzle_diameter": 0.4,
        "active_materials": ["PLA"],
    }
    facts.update(overrides)
    return facts


def _codes(result):
    return [reason["code"] for reason in result["reasons"]]


def test_known_x1c_alias_is_compatible():
    result = evaluate_education_compatibility(_file(), _printer(machine_type="O1D"))
    assert result == {
        "compatible": True,
        "engine_version": ENGINE_VERSION,
        "reasons": [],
        "facts": result["facts"],
    }


def test_known_p1s_alias_is_compatible():
    result = evaluate_education_compatibility(
        _file(machine=_fact("BL-P001", "printer_model")),
        _printer(machine_type="P1S"),
    )
    assert result["compatible"] is True


def test_rotated_bed_and_rounding_boundary_are_allowed():
    result = evaluate_education_compatibility(
        _file(bed=_fact({"x_mm": 210.5, "y_mm": 250.5}, "bed_shape")),
        _printer(bed_x_mm=250, bed_y_mm=210),
    )
    assert result["compatible"] is True
    assert result["facts"]["bed"]["rotation_used"] is True


def test_bed_above_rounding_boundary_fails():
    result = evaluate_education_compatibility(
        _file(bed=_fact({"x_mm": 210.51, "y_mm": 250.5}, "bed_shape")),
        _printer(bed_x_mm=250, bed_y_mm=210),
    )
    assert "bed_mismatch" in _codes(result)


def test_missing_api_provenance_fails_even_for_3mf_container():
    missing = _fact(None, "printer_model")
    missing.update(recognized=False, source_member=None, source_key=None)
    result = evaluate_education_compatibility(_file(api_types=missing), _printer())
    assert result["compatible"] is False
    assert "compatibility_unknown" in _codes(result)


def test_absent_machine_is_allowed_when_all_mandatory_facts_pass():
    absent = _fact(None, "printer_model")
    absent.update(recognized=False, source_member=None, source_key=None)
    result = evaluate_education_compatibility(_file(machine=absent), _printer())
    assert result["compatible"] is True


def test_present_unknown_machine_fails():
    result = evaluate_education_compatibility(
        _file(machine=_fact("Mystery Printer 9000", "printer_model")), _printer()
    )
    assert "compatibility_unknown" in _codes(result)


def test_present_mismatched_machine_fails():
    result = evaluate_education_compatibility(
        _file(machine=_fact("P1S", "printer_model")), _printer(machine_type="X1C")
    )
    assert "machine_mismatch" in _codes(result)


def test_missing_bed_nozzle_or_material_each_fails_closed():
    for key in ("bed", "nozzle", "materials"):
        absent = _fact(None, key)
        absent.update(recognized=False, source_member=None, source_key=None)
        result = evaluate_education_compatibility(_file(**{key: absent}), _printer())
        assert result["compatible"] is False, key
        assert "compatibility_unknown" in _codes(result), key


def test_nozzle_mismatch_fails_beyond_point_zero_one_mm():
    assert evaluate_education_compatibility(
        _file(nozzle=_fact(0.41, "nozzle_diameter")), _printer()
    )["compatible"] is True
    result = evaluate_education_compatibility(
        _file(nozzle=_fact(0.411, "nozzle_diameter")), _printer()
    )
    assert "nozzle_mismatch" in _codes(result)


def test_unrecognized_or_unloaded_material_fails():
    unknown = evaluate_education_compatibility(
        _file(materials=_fact(["Moon Dust"], "filament_type")), _printer()
    )
    assert "compatibility_unknown" in _codes(unknown)
    unloaded = evaluate_education_compatibility(
        _file(materials=_fact(["PETG"], "filament_type")), _printer(active_materials=["PLA"])
    )
    assert "material_mismatch" in _codes(unloaded)


def test_abrasive_material_fails_until_nozzle_material_is_modeled():
    result = evaluate_education_compatibility(
        _file(materials=_fact(["PLA-CF"], "filament_type")),
        _printer(active_materials=["PLA_CF"]),
    )
    assert "compatibility_unknown" in _codes(result)


def test_dispatch_recomputation_detects_slot_drift():
    approval = evaluate_education_compatibility(_file(), _printer(active_materials=["PLA"]))
    dispatch = evaluate_education_compatibility(_file(), _printer(active_materials=["PETG"]))
    assert approval["compatible"] is True
    assert dispatch["compatible"] is False
    assert "material_mismatch" in _codes(dispatch)
