"""Validate archived plate selection against actual stored print members."""
import re
import zipfile
from fastapi import HTTPException


def validate_plate(path: str, plate_index: int, api_type: str) -> int:
    """Return the one-based Bambu member number, never trust cached plate_count."""
    if type(plate_index) is not int or plate_index < 0:
        raise HTTPException(400, "Plate index must be a nonnegative integer")
    if api_type != "bambu":
        if plate_index != 0:
            raise HTTPException(400, "Plate selection is supported only for Bambu printers")
        return 1
    try:
        with zipfile.ZipFile(path) as package:
            members = package.infolist()
            numbers = []
            for member in members:
                match = re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.gcode", member.filename)
                if match:
                    if member.is_dir() or member.file_size == 0:
                        raise HTTPException(400, "Selected archive contains an empty print plate")
                    numbers.append(int(match.group(1)))
            if len(numbers) != len(set(numbers)):
                raise HTTPException(400, "Archive contains ambiguous duplicate print plates")
            # UI index N maps to protocol member N+1, never select by ZIP order.
            if plate_index + 1 not in numbers:
                raise HTTPException(400, "Selected print plate is not present in the stored file")
    except (OSError, zipfile.BadZipFile):
        raise HTTPException(400, "Original file is not a readable sliced Bambu 3MF") from None
    return plate_index + 1


def validate_mapping(mapping, filaments_json, slots, api_type):
    """Translate explicit zero-based source / one-based destination slots."""
    import json
    from core.material_identity import canonical_reported_material
    if mapping is None:
        return None
    if api_type != "bambu":
        raise HTTPException(400, "AMS mapping is supported only for Bambu printers")
    try:
        filaments = json.loads(filaments_json or "[]")
    except (TypeError, ValueError):
        raise HTTPException(400, "Original filament metadata is invalid") from None
    if not isinstance(filaments, list) or not filaments or len(filaments) > 16:
        raise HTTPException(400, "Original filament metadata cannot support AMS mapping")
    if not isinstance(mapping, list) or len(mapping) != len(filaments):
        raise HTTPException(400, "AMS mapping must cover every source filament")
    if not isinstance(slots, dict):
        raise HTTPException(400, "Selected tray material facts are unavailable")
    result = [-1] * len(filaments)
    destinations = set()
    for item in mapping:
        if not isinstance(item, dict) or set(item) != {"source_slot", "target_slot"}:
            raise HTTPException(400, "Invalid AMS mapping entry")
        source, target = item["source_slot"], item["target_slot"]
        if (type(source) is not int or type(target) is not int
                or not 0 <= source < len(result) or target not in slots
                or not 1 <= target <= 16 or result[source] != -1 or target in destinations):
            raise HTTPException(400, "Invalid or duplicate AMS slot")
        source_material = canonical_reported_material(
            filaments[source].get("type") if isinstance(filaments[source], dict) else None)
        target_material = canonical_reported_material(slots[target])
        if source_material is None or target_material is None or source_material != target_material:
            raise HTTPException(400, "Selected tray material does not match the source filament")
        result[source] = target - 1
        destinations.add(target)
    return result
