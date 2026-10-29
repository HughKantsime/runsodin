"""Physical slot identity for Bambu AMS units.

Bambu numbers four-tray units (AMS, AMS 2 Pro, AMS Lite) 0-3 and single-tray
AMS HT units 128-135 (captured H2D: unit "128", module n3s/128). Slots 1-16
are the four-tray units; AMS HT unit 128+n is slot 17+n, matching the bit
position Bambu uses for it in ``tray_exist_bits`` (captured H2D: 0x100ff).

Dispatch AMS mapping stays capped to slots 1-16 (Bambu tray index 0-15):
ams_evidence rejects AMS HT units, so no mapping target can reach an HT
slot. AMS HT is inventory/display only until HT acquisition evidence is
accepted; mapping it would need Bambu's HT tray id (the unit id, 128+).
"""
from modules.printers.telemetry.validators import coerce_int_or_raise

AMS_HT_FIRST_UNIT = 128
AMS_HT_UNIT_COUNT = 8
AMS_HT_FIRST_SLOT = 17


def ams_slot_number(unit_id, tray_id):
    """No array-position fallback; unknown hardware IDs return None."""
    try:
        unit = coerce_int_or_raise(unit_id)
        tray = coerce_int_or_raise(tray_id)
    except ValueError:
        return None
    if 0 <= unit <= 3 and 0 <= tray <= 3:
        return unit * 4 + tray + 1
    if AMS_HT_FIRST_UNIT <= unit < AMS_HT_FIRST_UNIT + AMS_HT_UNIT_COUNT and tray == 0:
        return AMS_HT_FIRST_SLOT + unit - AMS_HT_FIRST_UNIT
    return None


def is_rfid_tag(tag_uid):
    """Bambu reports non-RFID spools with an all-zero placeholder tag_uid."""
    return isinstance(tag_uid, str) and bool(tag_uid.strip()) and tag_uid.strip().strip('0') != ''


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


def tray_exists(bits, slot_number):
    try:
        return bool(int(bits, 16) & (1 << (slot_number - 1))) if isinstance(bits, str) else None
    except ValueError:
        return None


def inventory_report(units, bits=None, previous=()):
    """Keep a removal seen between heartbeat writes, including bit-only reports."""
    import copy
    result = copy.deepcopy(units)
    prior = dict(ams_trays_by_slot(list(previous)))
    for number, tray in ams_trays_by_slot(result):
        exists = tray_exists(bits, number)
        old = prior.get(number, {})
        tray['_exists'] = exists if exists is not None else old.get('_exists')
        tray['_removed'] = bool(old.get('_removed') or old.get('_exists') is False)
    return result
