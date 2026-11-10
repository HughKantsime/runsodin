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
import re

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


_HEX_COLOR = re.compile(r'#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?')


def hex_color(value):
    """RRGGBB from a 6- or 8-digit hex colour (optional '#'), or None.

    Alpha 00 is Bambu's no-colour placeholder (00000000 on loaded trays without
    a set colour), never a colour: it is None, not black.
    """
    match = _HEX_COLOR.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None or (match.group(2) or '').upper() == '00':
        return None
    return match.group(1).upper()


def reported_color(tray):
    """The tray's RRGGBB colour, or None when the report carries no colour.

    tray_color is RRGGBBAA; firmware that supports multi-colour trays repeats it
    in cols. An absent, malformed or placeholder tray_color falls back to cols[0].
    """
    cols = tray.get('cols')
    return hex_color(tray.get('tray_color')) or hex_color(cols[0] if isinstance(cols, list) and cols else None)


def _rfid(value):
    return value.strip().upper() if isinstance(value, str) and value.strip().strip('0') else None


def tray_swapped(tray, observation, destination, previous_spool):
    """Whether a different spool is in the tray than the slot last held.

    A tray resolved to a spool is a swap when that spool is not the slot's
    previous one. Otherwise the RFID identity is compared with the last
    observed one; an RFID spool replaced by a tagless one is a swap. A tagless
    tray after a tagless tray has unknown identity: not a swap (limitation:
    a different tagless spool of the same material keeps the known colour).
    """
    if destination is not None:
        return destination != previous_spool
    observed = (observation or {}).get('observed') or {}
    uuid, tag = _rfid(tray.get('tray_uuid')), _rfid(tray.get('tag_uid'))
    old_uuid, old_tag = _rfid(observed.get('uuid')), _rfid(observed.get('tag'))
    if uuid and old_uuid:
        return uuid != old_uuid
    if tag and old_tag:
        return tag != old_tag
    return bool(uuid or tag) != bool(old_uuid or old_tag)


def stored_observation(value):
    """A slot's stored tray observation, or {} when absent or unreadable."""
    import json
    try:
        result = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def slot_color(reported, filament_type, previous_type, previous_color, *, swapped=False, spool_color=None):
    """The slot colour for a loaded tray.

    A reported colour wins. Without one, a swapped spool takes that spool's own
    colour (or none); the same or an unknown spool keeps the known colour of
    the same loaded material.
    """
    if reported is not None:
        return reported
    if swapped:
        return hex_color(spool_color)
    return previous_color if filament_type == previous_type else None


def tray_exists(bits, slot_number):
    try:
        return bool(int(bits, 16) & (1 << (slot_number - 1))) if isinstance(bits, str) else None
    except ValueError:
        return None


def inventory_report(units, bits=None, previous=()):
    """Keep a removal seen between heartbeat writes, including bit-only reports."""
    import copy
    result = copy.deepcopy(units)
    # A malformed earlier report is no prior state, and must not refuse every later packet.
    prior = dict(ams_trays_by_slot(list(previous) if isinstance(previous, (list, tuple)) else []))
    for number, tray in ams_trays_by_slot(result):
        exists = tray_exists(bits, number)
        old = prior.get(number, {})
        tray['_exists'] = exists if exists is not None else old.get('_exists')
        tray['_removed'] = bool(old.get('_removed') or old.get('_exists') is False)
    return result
