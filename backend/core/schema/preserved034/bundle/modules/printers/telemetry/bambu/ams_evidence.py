"""Raw-only AMS facts. These facts do not prove mechanical feed readiness."""
from dataclasses import dataclass
from typing import Literal

from core.material_identity import canonical_reported_material

from modules.printers.telemetry.bambu.ams_identity import ams_trays_by_slot, ams_slot_number

@dataclass(frozen=True)
class RawTrayFact:
    slot_number: int
    unit_id: int
    tray_id: int
    material: str | None
    material_state: Literal['reported', 'explicit_empty', 'unknown', 'invalid']
    # Firmware material labels cannot establish actual filament occupancy.
    mechanically_ready: None = None


def parse_raw_ams_facts(print_packet) -> tuple[RawTrayFact, ...]:
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
    if len(root['ams']) > 4:
        raise ValueError('AMS packet identity unavailable')
    if any(not isinstance(unit, dict) or not isinstance(unit.get('tray'), list)
           or len(unit['tray']) > 4 for unit in root['ams']):
        raise ValueError('AMS packet identity unavailable')
    unit_ids = []
    for unit in root['ams']:
        identity = ams_slot_number(unit.get('id'), 0)
        # Acquisition evidence models four-tray units 0-3 only (unit/tray
        # derived from the slot number); AMS HT units are not accepted here.
        if identity is None or identity > 16 or identity in unit_ids:
            raise ValueError('AMS packet identity unavailable')
        unit_ids.append(identity)
    try:
        entries = ams_trays_by_slot(root['ams'], strict=True)
    except ValueError:
        raise ValueError('AMS packet identity unavailable') from None
    result = []
    for slot, tray in entries:
        value = tray.get('tray_type')
        material = canonical_reported_material(value)
        if 'tray_type' not in tray:
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
        result.append(RawTrayFact(slot, (slot - 1) // 4, (slot - 1) % 4,
                                  material, state))
    return tuple(sorted(result, key=lambda fact: fact.slot_number))
