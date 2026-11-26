"""Bambu installed-nozzle facts. No cached observation grants dispatch authority.

Decoding follows BambuStudio DeviceCore/DevNozzleSystem.cpp s_parse_nozzle_type:
second character = flow; suffix 00/01/05 = stainless/hardened/tungsten carbide.
The prefix is NOT a material code. Diameter always comes from its own field.
"""
import math
from core.h3c_plate import RackMessage
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


def rack_slot_id(position):
    """Raw filament slot for a numbered H3C rack position."""
    return 256 - position


def rack_rows_with_mounted_tool(rows):
    """Map the head's ID 0 back to the missing rack ID in a rack-only report."""
    if not isinstance(rows, list) or len(rows) < 2:
        return rows, None
    parked = set()
    mounted = None
    for row in rows:
        identifier = row.get('id') if isinstance(row, dict) else None
        if type(identifier) is not int:
            return rows, None
        if 16 <= identifier <= 24:
            parked.add(identifier)
        elif identifier == 0 and mounted is None:
            address = row.get('addr_id')
            if type(address) is not int or not 0 <= address <= 8:
                return rows, None
            mounted = 16 + address
        else:
            return rows, None
    if mounted is None or mounted in parked:
        return rows, None
    return [dict(row, id=mounted) if row['id'] == 0 else row for row in rows], mounted


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
    source = nozzle.get('src_id') if include_rack and isinstance(nozzle, dict) else None
    rows, mounted = rack_rows_with_mounted_tool(rows) if include_rack else (rows, None)
    if mounted is not None:
        source = mounted
    if type(exist) is int or type(source) is int:
        ids = {row.get('id') for row in rows if isinstance(row, dict) and type(row.get('id')) is int}
        expected = list(range(2 if dual else 1)) if type(exist) is int and mounted is None else []
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
            if identity['position'] is not None:
                identity['virtual_slot_id'] = rack_slot_id(identity['position'])
            if type(source) is int:
                identity['mounted'] = identifier == source
        else:
            continue
        code = row.get('type')
        material, flow = decode_type(code) if isinstance(code, str) else (None, None)
        present = bool(exist & (1 << identifier)) if type(exist) is int else None
        if identifier == mounted:
            present = True
        elif identity['side'] == 'rack' and type(exist) is int and exist < 0:
            present = None
        result.append({**identity, 'diameter': number(row.get('diameter'), positive=True),
                       'flow': flow, 'material': material, 'wear': number(row.get('wear')), 'present': present,
                       'type': code if isinstance(code, str) and len(code) <= 30 else None,
                       'observed_at': stamp})
    return result


def display_nozzles(rows, rack):
    """Keep the undecoded rack status in storage only."""
    objects = (row for row in rows or [] if isinstance(row, dict) and row)
    if not rack:
        return [row for row in objects if row.get('side') != 'rack']
    return sorted(({k: v for k, v in row.items() if k != 'stat'}
                   for row in objects if row.get('side') == 'rack' and row.get('position') is not None), key=lambda row: row['position'])


def compare_nozzles(requirements, installed):
    """(safe mismatch detail, incomplete). Compare every known requirement."""
    incomplete = not requirements
    by_id = {row['extruder']: row for row in installed if row.get('side') != 'rack'}
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


def rack_tools(installed):
    if isinstance(installed, str):
        import json
        try:
            installed = json.loads(installed)
        except ValueError:
            installed = None
    return installed if isinstance(installed, (list, tuple)) else ()


def position_refusal(source, position, installed):
    tools = [row for row in installed if isinstance(row, dict) and row.get('side') == 'rack'
             and row.get('position') == position]
    if len(tools) != 1 or tools[0].get('present') is not True:
        return RackMessage('nozzle_mismatch', f'Position {position} needs a reported, installed tool.')
    tool = tools[0]
    diameter = number(tool.get('diameter'), positive=True)
    prefix = str(tool.get('type') or '')[:2]
    actual = {'HS': 'standard', 'HH': 'standard', 'HT': 'TPU'}.get(prefix)
    required = {'Standard': 'standard', 'TPU285': 'TPU'}.get(source.get('volume_type'))
    need = source.get('nozzle_diameter')
    if not need or not required:
        return RackMessage('nozzle_mismatch', 'Slice the plate for the H3C.')
    # No capture maps a nonempty 3D.uncalibrated_nozzles entry to a rack position for refusal.
    if actual == required and diameter == need:
        return None
    reason = f"Filament {source['source_index'] + 1} was sliced for a {need:g} mm {required} tool."
    if not actual or not diameter:
        return RackMessage('nozzle_mismatch', f'{reason} The tool specification at position {position} is unknown.')
    size = '' if diameter == need else f'{diameter:g} mm '
    return RackMessage('nozzle_mismatch', f'{reason} Position {position} holds a {size}{actual} tool.')


def rack_tool_refusal(sources, mapping, installed):
    """A selected onboard tray feeds the tool at the same numbered position."""
    tools = rack_tools(installed)
    used = set()
    for source in sources:
        index = source['source_index']
        if index >= len(mapping) or type(mapping[index]) is not int or mapping[index] < 0:
            return RackMessage('ams_mapping_required', f'Choose a position for filament {index + 1}.')
        position = mapping[index] + 1
        if position > 9:
            return RackMessage('onboard_tray_required', f'Filament {index + 1} needs an onboard tray in positions 1 to 9.')
        if position in used:
            return RackMessage('duplicate_position', f'Choose separate positions for the two filaments assigned to position {position}.')
        used.add(position)
        detail = position_refusal(source, position, tools)
        if detail:
            return detail
    return None
