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


def source_filaments(filaments_json):
    """Read file IDs, assigning positional IDs only to wholly legacy metadata."""
    import json
    try:
        filaments = json.loads(filaments_json or '[]')
        if not isinstance(filaments, list) or not 1 <= len(filaments) <= 16:
            raise ValueError()
        if any(not isinstance(f, dict) for f in filaments):
            raise ValueError()
        # Older installs stored no IDs. Never fill gaps in explicitly identified
        # metadata: mixed lists are ambiguous and must still fail closed.
        if all('slot' not in f for f in filaments):
            filaments = [{**f, 'slot': i + 1} for i, f in enumerate(filaments)]
        ids = [f['slot'] for f in filaments]
        if any(type(slot) is not int or not 1 <= slot <= 16 for slot in ids) or len(set(ids)) != len(ids):
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise HTTPException(400, 'Original filament metadata is invalid') from None
    return filaments


def validate_mapping(mapping, filaments_json, slots, api_type):
    """Map one-based file IDs to a protocol vector indexed by ID minus one.

    Legacy zero-based positions are accepted only as a complete mapping for a
    file whose positions agree with its IDs. Never mix numbering conventions.
    """
    from core.material_identity import canonical_reported_material
    if mapping is None:
        return None
    if api_type != 'bambu':
        raise HTTPException(400, 'AMS mapping is supported only for Bambu printers')
    filaments = source_filaments(filaments_json)
    if not isinstance(mapping, list) or len(mapping) != len(filaments):
        raise HTTPException(400, 'AMS mapping must cover every source filament')
    if not isinstance(slots, dict):
        raise HTTPException(400, 'Selected tray material facts are unavailable')
    for item in mapping:
        if not isinstance(item, dict) or set(item) != {'source_slot', 'target_slot'}:
            raise HTTPException(400, 'Invalid AMS mapping entry')
        if type(item['source_slot']) is not int or type(item['target_slot']) is not int:
            raise HTTPException(400, 'Invalid or duplicate AMS slot')
    sources = {item['source_slot'] for item in mapping}
    by_id = {f['slot']: f for f in filaments}
    legacy = (sources == set(range(len(filaments)))
              and all(f['slot'] == i + 1 for i, f in enumerate(filaments)))
    if sources != set(by_id) and not legacy:
        raise HTTPException(400, 'Invalid or duplicate AMS slot')
    result = [-1] * max(by_id)
    for item in mapping:
        source = item['source_slot'] + (1 if legacy else 0)
        target = item['target_slot']
        if target not in slots or not 1 <= target <= 16 or result[source - 1] != -1:
            raise HTTPException(400, 'Invalid or duplicate AMS slot')
        source_material = canonical_reported_material(by_id[source].get('type'))
        target_material = canonical_reported_material(slots[target])
        if source_material is None or target_material is None or source_material != target_material:
            raise HTTPException(400, f'AMS slot {target}: selected tray material does not match source filament {source}')
        result[source - 1] = target - 1
    return result
