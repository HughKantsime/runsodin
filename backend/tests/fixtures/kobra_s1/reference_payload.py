"""Pure payload oracle copied from kobra-s1-screen e8ed872 files.py; no device I/O."""
import re
import posixpath

def safe_name(name):
    if not isinstance(name, str) or not name or '\x00' in name or '\\' in name or name.startswith('/') or any(p in ('..', '') for p in name.split('/')):
        raise ValueError('Invalid file path')
    return name

def rgb(color):
    if isinstance(color, str):
        return tuple(int(color[i:i+2],16) for i in (1,3,5))
    if isinstance(color, dict):
        return tuple(color[k] for k in ('R','G','B'))
    return tuple(color[:3])

def occupied(slot):
    return bool(slot.get('type') and slot.get('color')) and slot.get('status') == 'ready'

def mapping_warnings(filaments, slots, mapping):
    warnings = []
    for i,paint in enumerate(filaments):
        n = mapping[i] if i < len(mapping) else None
        if type(n) is not int or not 0 <= n < min(4,len(slots)) or not occupied(slots[n]):
            warnings.append('Filament %d: choose a ready ACE slot' % (i+1))
        elif slots[n]['type'].upper() != paint['type'].upper():
            warnings.append('Filament %d: material mismatch' % (i+1))
    return warnings

def build_payload(file, slots, mapping, settings, timestamp, msgid):
    filaments = file['filaments']
    if not filaments:
        raise ValueError('File filament header unavailable')
    warnings = mapping_warnings(filaments,slots,mapping)
    if warnings or len(mapping)!=len(filaments):
        raise ValueError('; '.join(warnings) or 'Incomplete mapping')
    if set(settings) != {'auto_leveling','flow_calibration','vibration_compensation'} or any(type(v) is not bool for v in settings.values()):
        raise ValueError('Invalid task settings')
    name = safe_name(file['path'])
    if not re.fullmatch('[a-f0-9]{32}',file['md5']) or type(file['size']) is not int or file['size'] <= 0:
        raise ValueError('Invalid file identity')
    # Field set and encodings match the start request proven on the S1 on
    # October 22 (see ODIN anycubic-lan-adapter spec): filepath is relative to
    # printer storage, colors are [R,G,B,A] arrays, task settings are 0/1 ints.
    def rgba(value):
        return list(rgb(value)) + [255]
    folder = posixpath.dirname(name)
    return dict(type='print', action='start', timestamp=timestamp, msgid=msgid, data=dict(
        taskid='-1', filename=posixpath.basename(name), filepath='/' + folder if folder else '/',
        filetype=1, md5=file['md5'], filesize=file['size'], project_type=1, task_mode=1,
        ams_settings=dict(use_ams=True, ams_box_mapping=[dict(paint_index=i, ams_index=n,
            paint_color=rgba(p['color']), ams_color=rgba(slots[n]['color']), material_type=p['type'])
            for i, (p, n) in enumerate(zip(filaments, mapping))]),
        task_settings={k: int(v) for k, v in settings.items()}))
