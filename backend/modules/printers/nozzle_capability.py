"""Bambu installed-nozzle facts. No cached observation grants dispatch authority.

Decoding follows BambuStudio DeviceCore/DevNozzleSystem.cpp s_parse_nozzle_type:
second character = flow; suffix 00/01/05 = stainless/hardened/tungsten carbide.
The prefix is NOT a material code. Diameter always comes from its own field.
"""
import math
from datetime import datetime, timezone


def number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def decode_type(code):
    legacy = {'hardened_steel': 'Hardened steel', 'stainless_steel': 'Stainless steel',
              'tungsten_carbide': 'Tungsten carbide'}
    if code in legacy:
        return legacy[code], None
    if not isinstance(code, str) or len(code) != 4:
        return None, None
    material = {'00': 'Stainless steel', '01': 'Hardened steel', '05': 'Tungsten carbide'}.get(code[2:])
    flow = {'S': 'Standard', 'H': 'High Flow'}.get(code[1])
    return material, flow


def installed_nozzles(packet, *, dual=False, observed_at=None):
    """Parse one packet, never copy top-level primary data to a second nozzle."""
    stamp = observed_at or datetime.now(timezone.utc).isoformat()
    device = packet.get('device')
    nozzle = device.get('nozzle', {}) if isinstance(device, dict) else {}
    rows = nozzle.get('info') if isinstance(nozzle, dict) else None
    exist = nozzle.get('exist') if isinstance(nozzle, dict) else None
    dual = dual or (type(exist) is int and bool(exist & 2))
    if not isinstance(rows, list):
        rows = ([{'id': 0, 'type': packet.get('nozzle_type'), 'diameter': packet.get('nozzle_diameter')}]
                if not dual and ('nozzle_type' in packet or 'nozzle_diameter' in packet) else [])
    exist = nozzle.get('exist') if isinstance(nozzle, dict) else None
    dual = dual or (type(exist) is int and bool(exist & 2)) or any(isinstance(row, dict) and row.get('id') == 1 for row in rows)
    if type(exist) is int:
        ids = {row.get('id') for row in rows if isinstance(row, dict) and type(row.get('id')) is int}
        rows = rows + [{'id': index} for index in range(2 if dual else 1) if index not in ids]
    result = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] not in (0, 1):
            continue  # rack IDs are not installed extruders
        code = row.get('type')
        material, flow = decode_type(code) if isinstance(code, str) else (None, None)
        exist = nozzle.get('exist') if isinstance(nozzle, dict) else None
        present = bool(exist & (1 << row['id'])) if type(exist) is int else None
        result.append({'extruder': row['id'], 'side': ('right' if row['id'] == 0 else 'left') if dual else 'single',
                       'diameter': number(row.get('diameter'), positive=True), 'flow': flow,
                       'material': material, 'wear': number(row.get('wear')), 'present': present,
                       'type': code if isinstance(code, str) and len(code) <= 30 else None,
                       'observed_at': stamp})
    return result


def compare_nozzles(requirements, installed):
    """(safe mismatch detail, incomplete). Compare every known requirement."""
    incomplete = not requirements
    by_id = {row['extruder']: row for row in installed}
    for required in requirements:
        actual = by_id.get(required['extruder'])
        side = required['side']
        if actual is None:
            incomplete = True
            continue
        if actual['present'] is False:
            return f'{side} nozzle required; installed nozzle is absent', incomplete
        for field in ('diameter', 'flow'):
            need, have = required.get(field), actual.get(field)
            if need is None or have is None:
                incomplete = True
            elif (abs(need - have) > 0.001 if field == 'diameter' else need != have):
                unit = ' mm' if field == 'diameter' else ''
                return f'{side} nozzle requires {need}{unit}; installed {have}{unit}', incomplete
        if required.get('hrc', 0):
            if actual['material'] == 'Stainless steel':
                return f'{side} nozzle requires hardened material (HRC {required["hrc"]:g}); installed Stainless steel', incomplete
            if actual['material'] is None:
                incomplete = True
    return None, incomplete
