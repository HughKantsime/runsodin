"""Raw-only AMS facts. These facts do not prove mechanical feed readiness."""
from dataclasses import dataclass
import re
from typing import Literal

from core.filament_color import hex_color
from core.material_identity import canonical_reported_material

from modules.printers.telemetry.bambu.ams_identity import (
    AMS_HT_FIRST_SLOT, AMS_HT_UNIT_COUNT, ams_trays_by_slot, ams_slot_number, tray_exists, unit_and_tray,
)

@dataclass(frozen=True)
class RawTrayFact:
    slot_number: int
    unit_id: int
    tray_id: int
    material: str | None
    material_state: Literal['reported', 'explicit_empty', 'unknown', 'invalid']
    # Firmware material labels cannot establish actual filament occupancy.
    mechanically_ready: None = None
    extruder: int | None = None
    color_hex: str | None = None


def parse_raw_ams_facts(print_packet, *, h3c=False) -> tuple[RawTrayFact, ...]:
    """Parse only explicitly reported tray fields; no cache or snapshot claims.

    Missing AMS data produces no facts. Invalid batch identity is refused as a
    whole. Explicitly addressed trays with missing/bad material produce an
    ineligible fact, allowing downstream latest-observation fencing.
    """
    if not isinstance(print_packet, dict):
        raise ValueError('AMS packet shape unavailable')
    if 'ams' not in print_packet:
        return ()
    root = print_packet['ams']
    if not isinstance(root, dict) or not isinstance(root.get('ams'), list):
        raise ValueError('AMS packet shape unavailable')
    if len(root['ams']) > 4 + AMS_HT_UNIT_COUNT:
        raise ValueError('AMS packet identity unavailable')
    if any(not isinstance(unit, dict) or not isinstance(unit.get('tray'), list)
           or len(unit['tray']) > (9 if h3c else 4) for unit in root['ams']):
        raise ValueError('AMS packet identity unavailable')
    unit_ids = []
    for unit in root['ams']:
        identity = ams_slot_number(unit.get('id'), 0, h3c=h3c)
        # Four-tray units 0-3 and AMS HT units 128-135 are both known hardware;
        # an H2D reports its AMS HT in every AMS packet. Unknown ids refuse.
        if identity is None or identity in unit_ids:
            raise ValueError('AMS packet identity unavailable')
        unit_ids.append(identity)
    try:
        entries = ams_trays_by_slot(root['ams'], strict=True, h3c=h3c)
    except ValueError:
        raise ValueError('AMS packet identity unavailable') from None
    # BambuStudio DevFilaSystemParser::ParseAmsInfo: info bits 8-11 are
    # the bound physical extruder. Missing info and switcher bindings are unknown.
    bindings = {}
    for unit in root['ams']:
        info = None if h3c else unit.get('info')  # A rack position is not a dual-nozzle binding.
        extruder = (int(info, 16) >> 8) & 0xf if type(info) is str and re.fullmatch('[0-9a-fA-F]{1,8}', info) else None
        bindings[int(unit['id'])] = extruder if extruder in (0, 1) else None
    result = []
    for slot, tray in entries:
        unit, tray_id = unit_and_tray(slot, h3c=h3c)
        value = tray.get('tray_type')
        material = canonical_reported_material(value)
        rack_empty = h3c and type(tray.get('state')) is int and tray['state'] == 0 and not value
        has_mask = h3c or slot >= AMS_HT_FIRST_SLOT
        if rack_empty or (has_mask and tray_exists(root.get('tray_exist_bits'), slot, h3c=h3c) is False):
            material, state = None, 'explicit_empty'
        elif 'tray_type' not in tray:
            state = 'unknown'
        elif not isinstance(value, str) or len(value) > 32 or any(ord(c) < 32 for c in value):
            state = 'invalid'
        elif not value.strip():
            state = 'explicit_empty'
        elif material is not None:
            state = 'reported'
        elif isinstance(value, str) and len(value) <= 32 and not any(ord(c) < 32 for c in value):
            state = 'unknown'
        else:
            state = 'invalid'
        result.append(RawTrayFact(slot, unit, tray_id, material, state, extruder=bindings.get(unit),
                                  color_hex=hex_color(tray.get('tray_color')) if h3c else None))
    return tuple(sorted(result, key=lambda fact: fact.slot_number))
