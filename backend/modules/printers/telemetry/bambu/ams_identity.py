"""Physical slot identity for the currently supported four-unit AMS surface."""
from modules.printers.telemetry.validators import coerce_int_or_raise


def ams_slot_number(unit_id, tray_id):
    """No array-position fallback; additional hardware IDs need acceptance."""
    try:
        unit = coerce_int_or_raise(unit_id)
        tray = coerce_int_or_raise(tray_id)
    except ValueError:
        return None
    if not (0 <= unit <= 3 and 0 <= tray <= 3):
        return None
    return unit * 4 + tray + 1


def ams_trays_by_slot(units, *, strict=False):
    """Refuse invalid inventory batches; omit ambiguous display/cache entries."""
    entries = []
    invalid = False
    seen = set()
    duplicate = set()
    if not isinstance(units, list):
        units = []
        invalid = True
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get('tray', []), list):
            invalid = True
            continue
        for tray in unit.get('tray', []):
            if not isinstance(tray, dict):
                invalid = True
                continue
            number = ams_slot_number(unit.get('id'), tray.get('id'))
            if number is None:
                invalid = True
                continue
            if number in seen:
                duplicate.add(number)
            seen.add(number)
            entries.append((number, tray))
    if strict and (invalid or duplicate):
        raise ValueError('AMS physical identity unavailable')
    return [(number, tray) for number, tray in entries if number not in duplicate]
