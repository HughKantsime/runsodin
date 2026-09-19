"""Versioned, fail-closed compatibility checks for Education submissions."""

from __future__ import annotations

import re
from typing import Any


ENGINE_VERSION = "education-compatibility-v1"
BED_TOLERANCE_MM = 0.5
NOZZLE_TOLERANCE_MM = 0.01


_MACHINE_ALIASES = {
    "x1c": "x1c",
    "x1carbon": "x1c",
    "bambulabx1c": "x1c",
    "bambulabx1carbon": "x1c",
    "c11": "x1c",
    "o1d": "x1c",
    "p1s": "p1s",
    "bambulabp1s": "p1s",
    "blp001": "p1s",
    "p1p": "p1p",
    "bambulabp1p": "p1p",
    "blp002": "p1p",
    "h2d": "h2d",
    "bambulabh2d": "h2d",
    "blp003": "h2d",
    "h2s": "h2s",
    "bambulabh2s": "h2s",
    "h2c": "h2c",
    "bambulabh2c": "h2c",
    "a1": "a1",
    "bambulaba1": "a1",
    "bla001": "a1",
    "a1mini": "a1-mini",
    "bambulaba1mini": "a1-mini",
    "bla003": "a1-mini",
    "k1": "k1",
    "crealityk1": "k1",
}

_MATERIAL_ALIASES = {
    "pla": "PLA",
    "plabasic": "PLA",
    "plamatte": "PLA",
    "plasilk": "PLA",
    "plaplus": "PLA",
    "pla+": "PLA",
    "petg": "PETG",
    "petgbasic": "PETG",
    "abs": "ABS",
    "asa": "ASA",
    "tpu": "TPU",
    "pa": "PA",
    "nylon": "PA",
    "pc": "PC",
    "pva": "PVA",
    "hips": "HIPS",
    "support": "SUPPORT",
    "plasupport": "PLA_SUPPORT",
    "plas": "PLA_SUPPORT",
    "placf": "PLA_CF",
    "petgcf": "PETG_CF",
    "pacf": "NYLON_CF",
    "pa6cf": "NYLON_CF",
    "pagf": "NYLON_GF",
    "pcabs": "PC_ABS",
    "pccf": "PC_CF",
    "pps": "PPS",
    "ppscf": "PPS_CF",
}

_ABRASIVE_MATERIALS = {
    "PLA_CF",
    "PETG_CF",
    "NYLON_CF",
    "NYLON_GF",
    "PC_CF",
    "PPS_CF",
}


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9+]", "", str(value or "").strip().lower())


def canonical_machine(value: Any) -> str | None:
    return _MACHINE_ALIASES.get(_token(value))


def canonical_material(value: Any) -> str | None:
    normalized = _token(value)
    if normalized in {"", "empty", "unknown", "other", "none"}:
        return None
    return _MATERIAL_ALIASES.get(normalized)


def _provenance_backed(fact: dict | None) -> bool:
    return bool(
        fact
        and fact.get("present")
        and fact.get("recognized")
        and str(fact.get("source_member") or "").strip()
        and str(fact.get("source_key") or "").strip()
    )


def _positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def evaluate_education_compatibility(
    file_facts: dict[str, Any] | None,
    printer_facts: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a deterministic compatibility decision and sanitized evidence."""
    source = file_facts or {}
    printer = printer_facts or {}
    reasons: list[dict[str, Any]] = []
    resolved: dict[str, Any] = {}

    def reject(code: str, detail: str, **values: Any) -> None:
        reasons.append({"code": code, "detail": detail, **values})

    api_fact = source.get("api_types") or {}
    api_type = str(printer.get("api_type") or "").strip().lower()
    api_values = {
        str(value).strip().lower()
        for value in (api_fact.get("value") or [])
        if str(value).strip()
    }
    resolved["api_type"] = {"required": sorted(api_values), "installed": api_type or None}
    if not _provenance_backed(api_fact) or not api_values:
        reject("compatibility_unknown", "API type is missing recognized file metadata provenance")
    elif not api_type:
        reject("compatibility_unknown", "Printer API type is unknown")
    elif api_type not in api_values:
        reject(
            "api_type_mismatch",
            "Printer API type is not authorized by the sliced file metadata",
            required=sorted(api_values),
            installed=api_type,
        )

    bed_fact = source.get("bed") or {}
    bed_value = bed_fact.get("value") or {}
    file_x = _positive(bed_value.get("x_mm"))
    file_y = _positive(bed_value.get("y_mm"))
    printer_x = _positive(printer.get("bed_x_mm"))
    printer_y = _positive(printer.get("bed_y_mm"))
    resolved["bed"] = {
        "required_mm": [file_x, file_y],
        "installed_mm": [printer_x, printer_y],
        "tolerance_mm": BED_TOLERANCE_MM,
    }
    if not _provenance_backed(bed_fact) or file_x is None or file_y is None:
        reject("compatibility_unknown", "File bed dimensions are missing recognized provenance")
    elif printer_x is None or printer_y is None:
        reject("compatibility_unknown", "Printer bed dimensions are unknown")
    else:
        normal = file_x <= printer_x + BED_TOLERANCE_MM and file_y <= printer_y + BED_TOLERANCE_MM
        rotated = file_x <= printer_y + BED_TOLERANCE_MM and file_y <= printer_x + BED_TOLERANCE_MM
        resolved["bed"]["rotation_used"] = bool(rotated and not normal)
        if not normal and not rotated:
            reject(
                "bed_mismatch",
                "Sliced bed dimensions do not fit this printer in either orientation",
            )

    nozzle_fact = source.get("nozzle") or {}
    file_nozzle = _positive(nozzle_fact.get("value"))
    printer_nozzle = _positive(printer.get("nozzle_diameter"))
    resolved["nozzle"] = {
        "required_mm": file_nozzle,
        "installed_mm": printer_nozzle,
        "tolerance_mm": NOZZLE_TOLERANCE_MM,
    }
    if not _provenance_backed(nozzle_fact) or file_nozzle is None:
        reject("compatibility_unknown", "Required nozzle diameter is missing recognized provenance")
    elif printer_nozzle is None:
        reject("compatibility_unknown", "Installed printer nozzle diameter is unknown")
    elif abs(file_nozzle - printer_nozzle) > NOZZLE_TOLERANCE_MM:
        reject("nozzle_mismatch", "Required and installed nozzle diameters differ")

    material_fact = source.get("materials") or {}
    raw_required = material_fact.get("value") or []
    if not isinstance(raw_required, list):
        raw_required = [raw_required]
    required_materials: list[str] = []
    invalid_required: list[str] = []
    for raw in raw_required:
        canonical = canonical_material(raw)
        if canonical is None:
            invalid_required.append(str(raw))
        elif canonical not in required_materials:
            required_materials.append(canonical)

    raw_active = printer.get("active_materials") or []
    active_materials = {
        canonical
        for canonical in (canonical_material(raw) for raw in raw_active)
        if canonical is not None
    }
    resolved["materials"] = {
        "required": required_materials,
        "active": sorted(active_materials),
    }
    if not _provenance_backed(material_fact) or not raw_required:
        reject("compatibility_unknown", "Required material metadata is missing recognized provenance")
    elif invalid_required or not required_materials:
        reject(
            "compatibility_unknown",
            "Required material metadata contains an empty or unrecognized material",
            unrecognized=invalid_required,
        )
    else:
        missing_materials = sorted(set(required_materials) - active_materials)
        if missing_materials:
            reject(
                "material_mismatch",
                "One or more required materials are not present in active printer slots",
                missing=missing_materials,
            )
        abrasive = sorted(set(required_materials) & _ABRASIVE_MATERIALS)
        if abrasive:
            reject(
                "compatibility_unknown",
                "Abrasive material requires nozzle-material data that ODIN does not model yet",
                materials=abrasive,
            )

    machine_fact = source.get("machine") or {}
    raw_file_machine = machine_fact.get("value")
    file_machine_present = bool(machine_fact.get("present") and str(raw_file_machine or "").strip())
    printer_machine_raw = (
        printer.get("machine_type") or printer.get("model") or printer.get("name")
    )
    file_machine = canonical_machine(raw_file_machine) if file_machine_present else None
    printer_machine = canonical_machine(printer_machine_raw)
    resolved["machine"] = {
        "required": file_machine,
        "installed": printer_machine,
        "required_present": file_machine_present,
    }
    if file_machine_present:
        if not _provenance_backed(machine_fact) or file_machine is None:
            reject("compatibility_unknown", "File machine identifier is unrecognized")
        elif printer_machine is None:
            reject("compatibility_unknown", "Printer machine identifier is unrecognized")
        elif file_machine != printer_machine:
            reject("machine_mismatch", "Sliced and installed printer models do not match")

    return {
        "compatible": not reasons,
        "engine_version": ENGINE_VERSION,
        "reasons": reasons,
        "facts": resolved,
    }
