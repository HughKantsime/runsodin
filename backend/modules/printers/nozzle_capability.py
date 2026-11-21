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


def installed_nozzles(packet, *, dual=False, include_rack=False, observed_at=None):
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
    dual = dual or any(isinstance(row, dict) and row.get('id') == 1 for row in rows)
    if type(exist) is int:
        ids = {row.get('id') for row in rows if isinstance(row, dict) and type(row.get('id')) is int}
        expected = list(range(2 if dual else 1))
        if include_rack:
            expected.extend(range(16, 25))
        rows = rows + [{'id': index} for index in expected if index not in ids]
    result = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('id')) is not int:
            continue
        identifier = row['id']
        if identifier in (0, 1):
            side = ('right' if identifier == 0 else 'left') if dual else 'single'
            identity = {'extruder': identifier, 'side': side}
        elif include_rack and 16 <= identifier <= 24:
            address = row.get('addr_id')
            identity = {'nozzle_id': identifier, 'side': 'rack',
                        'position': address + 1 if type(address) is int and 0 <= address < 9 else None,
                        'stat': row.get('stat') if type(row.get('stat')) is int else None}
            # One known load suggests snow's high byte names a virtual slot.
            # Use only the reported ID for display until a second load confirms direction.
            if type(row.get('snow')) is int and 0 <= row['snow'] <= 0xffff:
                identity['virtual_slot_id'] = row['snow'] >> 8
        else:
            continue
        code = row.get('type')
        material, flow = decode_type(code) if isinstance(code, str) else (None, None)
        present = bool(exist & (1 << identifier)) if type(exist) is int and exist >= 0 else None
        result.append({**identity, 'diameter': number(row.get('diameter'), positive=True),
                       'flow': flow, 'material': material, 'wear': number(row.get('wear')), 'present': present,
                       'type': code if isinstance(code, str) and len(code) <= 30 else None,
                       'observed_at': stamp})
    return result


def display_nozzles(rows, rack):
    """Keep the undecoded rack status in storage only."""
    if not rack:
        return [row for row in rows or [] if row.get('side') != 'rack']
    return sorted(({k: v for k, v in row.items() if k != 'stat'}
                   for row in rows or [] if row.get('side') == 'rack'), key=lambda row: row['position'])


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
