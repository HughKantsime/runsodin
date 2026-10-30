"""Pure stock Anycubic LAN report parsing. Never retain raw info/URLs."""
from copy import deepcopy
import math

MODELS = {'20024': 'Kobra 3', '20025': 'Kobra S1', '20026': 'Kobra 3 Max',
          '20027': 'Kobra 3 V2', '20029': 'Kobra S1 Max'}
STATES = {'free': 'IDLE', 'preheating': 'HEATING', 'auto_leveling': 'LEVELING',
          'vibrating': 'HEATING', 'flow_calibrating': 'HEATING', 'printing': 'PRINTING',
          'pausing': 'PAUSED', 'paused': 'PAUSED', 'resuming': 'PRINTING',
          'resumed': 'PRINTING', 'stopping': 'STOPPING', 'stoped': 'STOPPING',
          'finished': 'FINISHED'}


def merge_known(old, new):
    """Null means omitted, including in partial nested ACE reports."""
    result = deepcopy(old)
    for key, value in new.items():
        if value is not None:
            result[key] = merge_known(result.get(key, {}), value) if isinstance(value, dict) else deepcopy(value)
    return result


def merge_boxes(old, updates):
    boxes = {b['id']: deepcopy(b) for b in old}
    for update in updates:
        if not isinstance(update, dict) or type(update.get('id')) is not int or update['id'] < 0:
            continue
        box = boxes.setdefault(update['id'], {'id': update['id'], 'slots': []})
        slots = {s['index']: s for s in box['slots']}
        for slot in update.get('slots') or []:
            if isinstance(slot, dict) and type(slot.get('index')) is int and 0 <= slot['index'] < 4:
                slots[slot['index']] = merge_known(slots.get(slot['index'], {}), {k: v for k, v in slot.items() if k in ('index', 'type', 'color', 'color_group', 'consumables_percent', 'status', 'sku', 'edit_status', 'icon_type')})
        box.update(merge_known(box, {k: v for k, v in update.items() if k in ('id', 'status', 'model_id', 'auto_feed', 'loaded_slot', 'temp', 'humidity', 'drying_status', 'feed_status')}))
        box['slots'] = [slots[i] for i in sorted(slots)]
    return [boxes[i] for i in sorted(boxes)]


def filament_slots(boxes):
    """Wire indices are zero based; ODIN filament_slots are one based."""
    result = []
    for box in boxes:
        for slot in box['slots']:
            material = slot.get('type')
            if material is None:
                continue
            rgb = slot.get('color')
            color = ''.join(f'{v:02X}' for v in rgb) if isinstance(rgb, list) and len(rgb) == 3 and all(type(v) is int and 0 <= v <= 255 for v in rgb) else None
            remaining = slot.get('consumables_percent')
            if remaining == 0 and not slot.get('sku'):
                remaining = None
            result.append({'slot_number': box['id'] * 4 + slot['index'] + 1,
                           'material': material or 'EMPTY', 'color_hex': color,
                           'remaining': remaining, 'loaded': bool(material) and slot.get('status') == 5,
                           'active': box.get('loaded_slot') == slot['index'],
                           'box_id': box['id'], 'slot_index': slot['index'],
                           'box_model': {40001: 'ACE Pro', 40002: 'ACE 2'}.get(box.get('model_id'), 'Unknown ACE')})
    return result


def valid_report(topic, payload):
    if not isinstance(payload, dict) or payload.get('code', 200) != 200:
        return False
    parts = topic.split('/')
    if len(parts) != 9 or parts[:5] != ['anycubic', 'anycubicCloud', 'v1', 'printer', 'public'] or parts[-1] != 'report':
        return False
    data = payload.get('data')
    if not isinstance(data, dict):
        return False
    kind = parts[-2]
    if kind == 'info':
        return isinstance(data.get('state'), str) or isinstance(data.get('project'), dict) or isinstance(data.get('last_project'), dict)
    if kind == 'multiColorBox':
        return isinstance(data.get('multi_color_box'), list)
    keys = {'tempature': ('curr_hotbed_temp', 'curr_nozzle_temp', 'curr_chamber_temp'),
            'fan': ('fan_speed_pct',), 'print': ('progress',)}.get(kind, ())
    return any(_number(data.get(k)) for k in keys)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def parse_report(topic, payload, previous=None):
    """Return whitelisted telemetry only; topic type is authoritative."""
    state = deepcopy(previous or {})
    if not valid_report(topic, payload):
        return state
    parts = topic.split('/')
    if len(parts) != 9 or parts[:5] != ['anycubic', 'anycubicCloud', 'v1', 'printer', 'public'] or parts[-1] != 'report':
        return state
    kind = parts[-2]
    data = payload.get('data')
    if not isinstance(data, dict):
        return state
    if kind not in ('info', 'tempature', 'fan', 'print', 'multiColorBox'):
        return state
    if kind == 'multiColorBox':
        boxes = data.get('multi_color_box')
        if isinstance(boxes, list):
            state['boxes'] = merge_boxes(state.get('boxes', []), boxes)
        return state
    if kind == 'info':
        if isinstance(data.get('version'), str):
            state['firmware_version'] = data['version']
        project = data.get('project')
        if isinstance(project, dict):
            raw = project.get('state')
            if isinstance(raw, str) and raw in STATES:
                state['internal_state'] = STATES[raw]
            else:
                state.setdefault('internal_state', 'IDLE')
            # Only the live project's pause flag is authoritative.
            pause = project.get('pause')
            if pause in (1, 2):
                state['internal_state'] = 'PAUSED'
            elif pause == 4:
                state['internal_state'] = 'STOPPING'
            elif pause == 3 or (pause == 0 and raw in ('paused', 'pausing', 'resuming', 'resumed')):
                state['internal_state'] = 'PRINTING'
            _progress(state, project)
        else:
            last = data.get('last_project')
            active = state.get('internal_state') in ('PRINTING', 'HEATING', 'LEVELING', 'HOMING', 'PAUSED')
            terminal = last.get('state') if isinstance(last, dict) else None
            if active and terminal in ('finished', 'stoped'):
                # Emit once for a job we observed; the next info reflects live state.
                state['internal_state'] = STATES[terminal]
                _progress(state, last)
            elif data.get('state') == 'free' or state.get('internal_state') in ('FINISHED', 'STOPPING'):
                state['internal_state'] = 'IDLE'
            else:
                state.setdefault('internal_state', 'IDLE')
    elif kind == 'print' and 'progress' in data:
        _progress(state, data)
    temps = data.get('temp', {}) if kind == 'info' else data if kind == 'tempature' else {}
    for wire, target in {'curr_hotbed_temp': 'bed_temp', 'curr_nozzle_temp': 'nozzle_temp',
                         'target_hotbed_temp': 'bed_target', 'target_nozzle_temp': 'nozzle_target',
                         'curr_chamber_temp': 'box_temp', 'target_chamber_temp': 'box_target'}.items():
        if isinstance(temps, dict) and _number(temps.get(wire)):
            state[target] = temps[wire]
    if _number(data.get('fan_speed_pct')):
        state['model_fan'] = data['fan_speed_pct']
    return state


def _progress(state, data):
    if isinstance(data.get('filename'), str):
        state['filename'] = data['filename']
    for wire, target in {'progress': 'progress_percent', 'curr_layer': 'current_layer'}.items():
        if _number(data.get(wire)):
            state[target] = data[wire]
    layers = data.get('total_layers', data.get('total_layer'))
    if _number(layers):
        state['total_layers'] = layers
    for wire, target in [('remain_time', 'time_remaining'), ('print_time', 'current_ticks')]:
        if _number(data.get(wire)):
            state[target] = data[wire] * 60
