"""Rinkhals ACE observations and credential-free gkapi start planning."""
import json
import posixpath
import re


class S1Refusal(ValueError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(f'{code}: {detail}')


def parse_ace(hub):
    hub = hub if isinstance(hub, dict) else {}
    hubs = hub.get('filament_hubs') or []
    unit = hubs[0] if isinstance(hubs, list) and hubs and isinstance(hubs[0], dict) else {}
    reported = unit.get('slots') or []
    by_index = {s.get('index'): s for s in reported if isinstance(s, dict) and type(s.get('index')) is int} if isinstance(reported, list) else {}
    slots = []
    for i in range(4):
        raw = by_index.get(i, {})
        material = raw.get('type')
        material = material.strip()[:64] if isinstance(material, str) and not any(ord(c) < 32 for c in material) else ''
        color = raw.get('color')
        color = color[:3] if isinstance(color, list) and len(color) >= 3 and all(type(n) is int and 0 <= n <= 255 for n in color[:3]) else None
        slots.append(dict(index=i, type=material, color=color,
                          color_hex=''.join(f'{n:02X}' for n in color) if color else None,
                          usable=bool(material) and raw.get('status') == 'ready',
                          status=raw.get('status') if raw.get('status') in ('ready', 'empty') else 'unknown'))
    current = hub.get('current_filament')
    current = current if isinstance(current, str) and (current == '' or re.fullmatch(r'\d+-\d+', current)) else None
    return dict(slots=slots, current_filament=current, humidity=None,
                auto_refill=hub.get('auto_refill') if type(hub.get('auto_refill')) is int and hub['auto_refill'] in (0, 1) else None,
                temperature=unit.get('temp') if type(unit.get('temp')) in (int, float) else None)


def parse_header(text):
    fields = {}
    for line in text.splitlines():
        match = re.match(r'^;\s*(filament_colour|filament_color|filament_type)\s*=\s*(.*)$', line)
        if match:
            fields[match[1]] = [s.strip().strip('"') for s in match[2].split(';')]
    colors = fields.get('filament_colour', fields.get('filament_color', []))
    types = fields.get('filament_type', [])
    if (not colors or len(colors) != len(types) or len(colors) > 64
            or any(not re.fullmatch(r'#[0-9a-fA-F]{6}', c) for c in colors) or any(not t for t in types)):
        raise S1Refusal('s1_file_filaments_unknown', 'File filament header unavailable')
    return [dict(color=c.upper(), type=t.upper()) for c, t in zip(colors, types)]


# Bambu Studio's curr_bed_type names the plate whose first-layer key gklib heats to.
_PLATE_KEYS = {'cool plate': 'cool_plate_temp_initial_layer', 'engineering plate': 'eng_plate_temp_initial_layer',
               'high temp plate': 'hot_plate_temp_initial_layer', 'textured pei plate': 'textured_plate_temp_initial_layer',
               'textured cool plate': 'textured_cool_plate_temp_initial_layer',
               'cool plate (supertack)': 'supertack_plate_temp_initial_layer', 'supertack plate': 'supertack_plate_temp_initial_layer'}


def require_start_metadata(text):
    """gklib reads the bed temperature only from a slicer header it recognizes.

    Without one it levels on a cold bed (Oct 28: target 5 C, 20-minute wait), and
    auto_leveling=0 does not prevent it. Only the proven Bambu Studio header passes.
    """
    lines = text.splitlines()
    if not any(re.match(r'^;\s*BambuStudio\s+\d', line) for line in lines[:20]):
        raise S1Refusal('s1_file_start_metadata_missing', 'S1 files need a Bambu Studio header; other files make the printer level on a cold bed')
    fields = {}
    for line in lines:
        match = re.match(r'^;\s*([a-z_]+)\s*=\s*(.*)$', line)
        if match and match[1] not in fields:
            fields[match[1]] = match[2].strip()
    def first_positive(key):
        value = re.split(r'[,;]', fields.get(key, ''))[0].strip()
        return int(value) if re.fullmatch(r'\d{1,3}', value) and 0 < int(value) <= 150 else None
    plate = _PLATE_KEYS.get(fields.get('curr_bed_type', '').strip().strip('"').casefold())
    temperature = (first_positive(plate) if plate else None) or first_positive('first_layer_bed_temperature')
    if temperature is None:
        raise S1Refusal('s1_file_start_metadata_missing', 'S1 files need a first-layer bed temperature for the selected plate')
    return temperature


def read_filaments(path):
    with open(path, 'rb') as stream:
        text = stream.read(256 * 1024).decode('utf-8', 'replace')
    require_start_metadata(text)
    return parse_header(text)


def mapping_vector(value, filaments):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if value is None:
        raise S1Refusal('ams_mapping_required', 'Choose an ACE slot for every file filament')
    if (type(value) is not list or len(value) != len(filaments)
            or any(type(n) is not int or not 0 <= n < 4 for n in value)):
        raise S1Refusal('s1_mapping_incomplete', 'AMS mapping must cover every file filament with an ACE slot')
    # Bambu sources may share a tray since 2.0.36. gklib is proven with one slot per
    # paint index only, so the shared validate_mapping no longer enforces this for us.
    if len(set(value)) != len(value):
        raise S1Refusal('s1_mapping_duplicate_slot', 'Each file filament needs its own ACE slot')
    return value


def verify_mapping(filaments, slots, mapping):
    from core.print_payload import validate_mapping
    from fastapi import HTTPException
    mapping = mapping_vector(mapping, filaments)
    for n in mapping:
        if n >= len(slots) or not slots[n].get('usable'):
            raise S1Refusal('s1_slot_unusable', f'Slot {n + 1} is empty or unknown')
        if not slots[n].get('color'):
            raise S1Refusal('s1_slot_color_unknown', f'Slot {n + 1} color is unknown')
    try:
        validate_mapping([dict(source_slot=i + 1, target_slot=n + 1) for i, n in enumerate(mapping)],
            json.dumps([dict(slot=i + 1, type=f['type']) for i, f in enumerate(filaments)]),
            {i + 1: s['type'] for i, s in enumerate(slots)}, 'bambu')
    except HTTPException:
        raise S1Refusal('s1_material_mismatch', 'Mapped ACE materials must match each file filament; unknown materials cannot be verified') from None
    return mapping


def safe_path(name):
    if not isinstance(name, str) or not name or '\x00' in name or '\\' in name or name.startswith('/') or any(p in ('..', '') for p in name.split('/')):
        raise S1Refusal('s1_file_identity_invalid', 'Invalid file path')
    return name


def build_payload(file, slots, mapping, timestamp, msgid):
    filaments = file['filaments']
    verify_mapping(filaments, slots, mapping)
    name = safe_path(file['path'])
    if not re.fullmatch('[a-f0-9]{32}', file['md5']) or type(file['size']) is not int or file['size'] <= 0:
        raise S1Refusal('s1_file_identity_invalid', 'Invalid device file identity')
    def rgba(value):
        return ([int(value[i:i+2], 16) for i in (1, 3, 5)] if isinstance(value, str) else value[:3]) + [255]
    return dict(type='print', action='start', timestamp=timestamp, msgid=msgid, data=dict(
        taskid='-1', filename=posixpath.basename(name), filepath='/' + posixpath.dirname(name),
        filetype=1, md5=file['md5'], filesize=file['size'], project_type=1, task_mode=1,
        ams_settings=dict(use_ams=True, ams_box_mapping=[dict(paint_index=i, ams_index=n,
            paint_color=rgba(p['color']), ams_color=rgba(slots[n]['color']), material_type=p['type'])
            for i, (p, n) in enumerate(zip(filaments, mapping))]),
        task_settings=dict(auto_leveling=0, vibration_compensation=0, flow_calibration=0)))
