"""Validate archived plate selection against actual stored print members."""
import re
import zipfile
from fastapi import HTTPException


def bambu_plate_numbers(path: str) -> list:
    """The stored package's Metadata/plate_N.gcode numbers, from one bounded open.

    The member count is checked from the end-of-central-directory record before
    zipfile lists the directory, so an absurd stored package is refused cheaply."""
    from core.zip_bounds import open_bounded
    try:
        with open_bounded(path) as package:
            numbers = []
            for member in package.infolist():
                match = re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.gcode", member.filename)
                if match:
                    if member.is_dir() or member.file_size == 0:
                        raise HTTPException(400, "Selected archive contains an empty print plate")
                    numbers.append(int(match.group(1)))
    except (OSError, zipfile.BadZipFile):
        raise HTTPException(400, "Original file is not a readable sliced Bambu 3MF") from None
    if len(numbers) != len(set(numbers)):
        raise HTTPException(400, "Archive contains ambiguous duplicate print plates")
    return sorted(numbers)


def validate_plate(path: str, plate_index: int, api_type: str, *, plate_numbers=None) -> int:
    """Return the one-based Bambu member number, never trust cached plate_count.
    Pass ``plate_numbers`` (from bambu_plate_numbers) to validate several plates of one
    package without reopening it."""
    if type(plate_index) is not int or plate_index < 0:
        raise HTTPException(400, "Plate index must be a nonnegative integer")
    if api_type != "bambu":
        if plate_index != 0:
            raise HTTPException(400, "Plate selection is supported only for Bambu printers")
        return 1
    numbers = bambu_plate_numbers(path) if plate_numbers is None else plate_numbers
    # UI index N maps to protocol member N+1, never select by ZIP order.
    if plate_index + 1 not in numbers:
        raise HTTPException(400, "Selected print plate is not present in the stored file")
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
