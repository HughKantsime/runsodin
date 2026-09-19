"""Provenance-preserving print-file safety metadata extraction.

The Education compatibility path treats every value returned in ``safety_facts``
as evidence. Missing metadata stays missing: this module never invents a nozzle
diameter or authorizes a printer API from a filename extension.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from typing import Any

from defusedxml import ElementTree as ET


log = logging.getLogger("odin.print_file_meta")

KNOWN_PRINTER_BEDS = {
    "x1 carbon": (256, 256),
    "x1c": (256, 256),
    "x1e": (256, 256),
    "x1": (256, 256),
    "p1s": (256, 256),
    "p1p": (256, 256),
    "a1 mini": (180, 180),
    "a1": (256, 256),
    "h2d": (320, 320),
    "mk4": (250, 210),
    "mk3": (250, 210),
    "mini": (180, 180),
    "ender 3": (220, 220),
    "ender-3": (220, 220),
    "voron": (300, 300),
}

_BAMBU_MACHINE_MARKERS = (
    "bambu lab",
    "x1 carbon",
    "x1c",
    "x1e",
    "p1s",
    "p1p",
    "a1 mini",
    "h2d",
    "h2s",
    "h2c",
    "bl-p001",
    "bl-p002",
    "bl-p003",
    "bl-a001",
    "bl-a003",
    "c11",
    "c12",
    "c13",
    "o1d",
)


def _missing_fact() -> dict[str, Any]:
    return {
        "present": False,
        "recognized": False,
        "source_member": None,
        "source_key": None,
        "value": None,
    }


def _fact(value: Any, member: str, key: str, *, recognized: bool = True) -> dict[str, Any]:
    return {
        "present": value is not None and value != "" and value != [],
        "recognized": recognized,
        "source_member": member,
        "source_key": key,
        "value": value,
    }


def _safe_float(value: Any) -> float | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        parsed = float(str(value).split(",", 1)[0].strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _material_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _bed_shape(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, list):
        value = ",".join(str(item) for item in value)
    coords: list[tuple[float, float]] = []
    for pair in str(value or "").split(","):
        match = re.fullmatch(r"\s*(-?[0-9.]+)x(-?[0-9.]+)\s*", pair)
        if not match:
            continue
        try:
            coords.append((float(match.group(1)), float(match.group(2))))
        except ValueError:
            continue
    if not coords:
        return None, None
    width = max(x for x, _ in coords) - min(x for x, _ in coords)
    depth = max(y for _, y in coords) - min(y for _, y in coords)
    return (width, depth) if width > 0 and depth > 0 else (None, None)


def _extract_gcode_meta(file_path: str):
    """Return legacy G-code bed dimensions without asserting API compatibility."""
    x = None
    y = None
    try:
        with open(file_path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 100:
                    break
                stripped = line.strip()
                if not stripped.startswith(";"):
                    continue
                content = stripped[1:].strip()
                for key, axis in (
                    ("bed_size_x", "x"),
                    ("bed_size_y", "y"),
                    ("machine_width", "x"),
                    ("machine_depth", "y"),
                    ("plate_x", "x"),
                    ("print_size_x", "x"),
                ):
                    if content.startswith(key):
                        try:
                            parsed = float(content.split("=", 1)[1].strip())
                        except (ValueError, IndexError):
                            break
                        if axis == "x":
                            x = parsed
                        else:
                            y = parsed
                        break
                if x is not None and y is not None:
                    break
    except Exception as exc:
        log.debug("[print_file_meta] gcode parse error for %s: %s", file_path, exc)
    return x, y


def _lookup_known_bed(model_str: str):
    model_lower = str(model_str or "").lower()
    for key, dims in KNOWN_PRINTER_BEDS.items():
        if key in model_lower:
            return dims
    return None, None


def _api_types_for_machine(machine: str | None) -> list[str]:
    normalized = str(machine or "").strip().lower()
    if normalized and any(marker in normalized for marker in _BAMBU_MACHINE_MARKERS):
        return ["bambu"]
    return []


def _first_project_fact(data: dict, keys: tuple[str, ...], member: str) -> dict[str, Any]:
    for key in keys:
        if key in data and str(data[key] or "").strip():
            return _fact(data[key], member, key)
    return _missing_fact()


def _extract_3mf_safety_facts(file_path: str) -> dict[str, dict[str, Any]]:
    """Extract safety facts and their exact recognized archive source."""
    facts = {
        "api_types": _missing_fact(),
        "machine": _missing_fact(),
        "bed": _missing_fact(),
        "nozzle": _missing_fact(),
        "materials": _missing_fact(),
    }
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = {name.lower(): name for name in zf.namelist()}

            project_member = names.get("metadata/project_settings.config")
            if project_member:
                try:
                    data = json.loads(zf.read(project_member).decode("utf-8", errors="replace"))
                    facts["machine"] = _first_project_fact(
                        data,
                        ("printer_model", "printer_model_id", "machine_model"),
                        project_member,
                    )
                    for key in ("nozzle_diameter", "nozzle_diameters"):
                        if key in data:
                            value = _safe_float(data[key])
                            if value is not None:
                                facts["nozzle"] = _fact(value, project_member, key)
                                break
                    if "filament_type" in data:
                        values = _material_values(data["filament_type"])
                        if values:
                            facts["materials"] = _fact(values, project_member, "filament_type")
                    for key in ("bed_shape", "printable_area"):
                        if key in data:
                            x, y = _bed_shape(data[key])
                            if x is not None and y is not None:
                                facts["bed"] = _fact(
                                    {"x_mm": x, "y_mm": y}, project_member, key
                                )
                                break
                except Exception as exc:
                    log.debug("[print_file_meta] project settings parse error: %s", exc)

            slice_member = names.get("metadata/slice_info.config")
            if slice_member:
                try:
                    content = zf.read(slice_member).decode("utf-8", errors="replace")
                    metadata: dict[str, str] = {}
                    filament_types: list[str] = []
                    try:
                        root = ET.fromstring(content)
                        for node in root.findall(".//metadata"):
                            key = node.get("key")
                            value = node.get("value")
                            if key and value:
                                metadata[key] = value
                        filament_types = [
                            str(node.get("type")).strip()
                            for node in root.findall(".//filament")
                            if str(node.get("type") or "").strip()
                        ]
                    except Exception:
                        for key in (
                            "machine_model",
                            "printer_model",
                            "printer_model_id",
                            "nozzle_diameters",
                        ):
                            match = re.search(
                                rf"(?im)^\s*{re.escape(key)}\s*=\s*[\"']?([^\"'\r\n]+)",
                                content,
                            )
                            if match:
                                metadata[key] = match.group(1).strip()

                    if not facts["machine"]["present"]:
                        for key in ("machine_model", "printer_model", "printer_model_id"):
                            if str(metadata.get(key, "")).strip():
                                facts["machine"] = _fact(metadata[key], slice_member, key)
                                break
                    if not facts["nozzle"]["present"]:
                        for key in ("nozzle_diameters", "nozzle_diameter"):
                            value = _safe_float(metadata.get(key))
                            if value is not None:
                                facts["nozzle"] = _fact(value, slice_member, key)
                                break
                    if not facts["materials"]["present"] and filament_types:
                        facts["materials"] = _fact(
                            filament_types, slice_member, "filament[].type"
                        )
                except Exception as exc:
                    log.debug("[print_file_meta] slice info parse error: %s", exc)

            plate_member = names.get("metadata/plate_1.json")
            if plate_member and not facts["nozzle"]["present"]:
                try:
                    plate_data = json.loads(
                        zf.read(plate_member).decode("utf-8", errors="replace")
                    )
                    value = _safe_float(plate_data.get("nozzle_diameter"))
                    if value is not None:
                        facts["nozzle"] = _fact(
                            value, plate_member, "nozzle_diameter"
                        )
                except Exception as exc:
                    log.debug("[print_file_meta] plate metadata parse error: %s", exc)

            model_member = names.get("metadata/model_settings.config")
            if model_member and not facts["bed"]["present"]:
                try:
                    content = zf.read(model_member).decode("utf-8", errors="replace")
                    match = re.search(r"(?im)^\s*bed_shape\s*=\s*([^\r\n]+)", content)
                    if match:
                        x, y = _bed_shape(match.group(1))
                        if x is not None and y is not None:
                            facts["bed"] = _fact(
                                {"x_mm": x, "y_mm": y}, model_member, "bed_shape"
                            )
                except Exception as exc:
                    log.debug("[print_file_meta] model settings parse error: %s", exc)

            machine = facts["machine"].get("value")
            if machine:
                api_types = _api_types_for_machine(str(machine))
                if api_types:
                    facts["api_types"] = _fact(
                        api_types,
                        str(facts["machine"]["source_member"]),
                        str(facts["machine"]["source_key"]),
                    )
    except Exception as exc:
        log.debug("[print_file_meta] 3mf open error for %s: %s", file_path, exc)
    return facts


def _extract_3mf_meta(file_path: str):
    bed = _extract_3mf_safety_facts(file_path)["bed"].get("value") or {}
    return bed.get("x_mm"), bed.get("y_mm")


def _resolve_api_types(extension: str, safety_facts: dict | None = None) -> str:
    """Return API types only when supported by recognized in-file provenance."""
    del extension
    api_fact = (safety_facts or {}).get("api_types") or {}
    if not api_fact.get("present") or not api_fact.get("recognized"):
        return ""
    values = api_fact.get("value") or []
    return ",".join(
        sorted({str(value).strip().lower() for value in values if str(value).strip()})
    )


def extract_print_file_meta(file_path: str, extension: str) -> dict:
    """Extract legacy columns plus provenance-rich safety facts; never raise."""
    ext = extension.lower().lstrip(".")
    bed_x = None
    bed_y = None
    facts = {
        "api_types": _missing_fact(),
        "machine": _missing_fact(),
        "bed": _missing_fact(),
        "nozzle": _missing_fact(),
        "materials": _missing_fact(),
    }
    try:
        if ext == "3mf":
            facts = _extract_3mf_safety_facts(file_path)
            bed = facts["bed"].get("value") or {}
            bed_x, bed_y = bed.get("x_mm"), bed.get("y_mm")
        elif ext == "gcode":
            bed_x, bed_y = _extract_gcode_meta(file_path)
            if bed_x is not None and bed_y is not None:
                facts["bed"] = _fact(
                    {"x_mm": bed_x, "y_mm": bed_y}, "gcode_header", "bed_dimensions"
                )
    except Exception as exc:
        log.warning("[print_file_meta] unexpected extraction error for %s: %s", file_path, exc)

    return {
        "bed_x_mm": bed_x,
        "bed_y_mm": bed_y,
        "compatible_api_types": _resolve_api_types(extension, facts),
        "safety_facts": facts,
    }
