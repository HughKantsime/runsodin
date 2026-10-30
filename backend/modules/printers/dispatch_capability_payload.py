"""Bounded, selected-plate nozzle and geometry inspection for Bambu dispatch."""
import json
from defusedxml import ElementTree as ET
from core.zip_bounds import open_bounded, check_expansion
from modules.printers.nozzle_capability import number
from modules.printers.printer_models import bambu_model_name, bambu_nozzle_count


def values(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return value.replace(',', ' ').split()
    return [] if value is None else [value]


def at(value, index):
    items = values(value)
    return items[index] if 0 <= index < len(items) else None


def flow(value):
    return {'0': 'Standard', '1': 'High Flow', 'Standard': 'Standard', 'High Flow': 'High Flow',
            'TPU High Flow': 'TPU High Flow', 'E3D High Flow': 'E3D High Flow'}.get(str(value))


def bounds(value):
    if not isinstance(value, list) or len(value) != 4:
        return None
    # Negative coordinates are real out-of-bed geometry, not missing evidence.
    try:
        import math
        parsed = [float(v) for v in value if not isinstance(v, bool)]
        if len(parsed) == 4 and all(math.isfinite(v) for v in parsed) and parsed[0] <= parsed[2] and parsed[1] <= parsed[3]:
            return parsed
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def inspect_payload(path, plate_number, model):
    """Read only metadata; no extraction, mesh parser, or unbounded G-code scan."""
    with open_bounded(path) as package:
        check_expansion(package)
        names = {name.lower(): name for name in package.namelist()}
        def read(name):
            member = names.get(name.lower())
            if member is None:
                return None
            if package.getinfo(member).file_size > 2 * 1024 * 1024:
                raise ValueError('Capability metadata exceeds inspection bound')
            return package.read(member)
        project_raw = read('Metadata/project_settings.config')
        project = json.loads(project_raw) if project_raw else {}
        plate_raw = read(f'Metadata/plate_{plate_number}.json')
        plate = json.loads(plate_raw) if plate_raw else {}
        slice_raw = read('Metadata/slice_info.config')
        selected = None
        if slice_raw:
            root = ET.fromstring(slice_raw)
            for candidate in root.findall('.//plate'):
                meta = {node.get('key'): node.get('value') for node in candidate.findall('metadata')}
                if str(meta.get('index', candidate.get('index'))) == str(plate_number):
                    selected = candidate
                    break
        dual = bambu_nozzle_count(model or project.get('printer_model')) == 2
        physical = values(project.get('physical_extruder_map'))
        dual = dual or len(physical) > 1
        # The slicer CLI leaves filament_nozzle_map at a one-entry default on a
        # multi-filament file, as ams_source_inspection._source_extruder also ignores.
        nozzle_map = values(project.get('filament_nozzle_map'))
        if len(nozzle_map) == 1 < len(values(project.get('filament_type'))):
            nozzle_map = []
        mapping = nozzle_map or values(project.get('filament_map'))
        requirements = []
        incomplete = False
        filament_to_physical = {}
        filament_requirements = {}
        # Slice-info contains only filaments actually used on this plate.
        for filament in ([] if selected is None else selected.findall('filament')):
            if all(number(filament.get(k)) == 0 for k in ('used_g', 'used_m')):
                continue
            try:
                index = int(filament.get('id')) - 1
                logical = int(at(mapping, index)) - 1 if dual else 0
                extruder = int(at(physical, logical)) if dual else 0
                if index < 0 or logical < 0 or extruder not in (0, 1):
                    incomplete = True
                    continue
            except (TypeError, ValueError):
                incomplete = True
                continue
            filament_to_physical[index] = extruder
            requirements.append({'extruder': extruder, 'side': ('right' if extruder == 0 else 'left') if dual else 'single',
                'diameter': number(filament.get('nozzle_diameter'), positive=True) or number(at(project.get('nozzle_diameter'), logical), positive=True),
                'flow': flow(filament.get('volume_type')) or flow(at(project.get('nozzle_volume_type'), logical)),
                'hrc': number(at(project.get('required_nozzle_HRC'), index)) or 0})
            filament_requirements[index] = requirements[-1]
        # plate_N.json filament_ids are zero-based; slice-info ids are one-based.
        plate_ids = plate.get('filament_ids')
        if isinstance(plate_ids, list):
            requirements = []
            for index in plate_ids:
                try:
                    if type(index) is not int or index < 0:
                        raise ValueError()
                    logical = int(at(mapping, index)) - 1 if dual else 0
                    extruder = int(at(physical, logical)) if dual else 0
                    if logical < 0 or extruder not in (0, 1):
                        raise ValueError()
                except (ValueError, TypeError):
                    incomplete = True
                    continue
                filament_to_physical[index] = extruder
                requirements.append(filament_requirements.get(index) or {'extruder': extruder, 'side': ('right' if extruder == 0 else 'left') if dual else 'single',
                    'diameter': number(at(project.get('nozzle_diameter'), logical), positive=True),
                    'flow': flow(at(project.get('nozzle_volume_type'), logical)),
                    'hrc': number(at(project.get('required_nozzle_HRC'), index)) or 0})
        if not dual and not requirements and project:
            requirements = [{'extruder': 0, 'side': 'single',
                'diameter': number(at(project.get('nozzle_diameter'), 0), positive=True),
                'flow': flow(at(project.get('nozzle_volume_type'), 0)),
                'hrc': max([number(v) or 0 for v in values(project.get('required_nozzle_HRC'))] or [0])}]
        return {'printer_model': project.get('printer_model'), 'notes': [], 'requirements': requirements, 'incomplete': incomplete, 'bbox': bounds(plate.get('bbox_all')),
                'objects': plate.get('bbox_objects', []), 'filament_map': filament_to_physical}


def geometry_refusal(facts, creds):
    """Owner's H2D reach: left X 0–325, right X 25–350 (2026-10-27 spec)."""
    from modules.printers.printer_models import BAMBU_BEDS
    model = bambu_model_name(creds.get('machine_type') or creds.get('model'))
    defaults = BAMBU_BEDS.get(model, (None, None))
    width, depth = creds.get('bed_x_mm') or defaults[0], creds.get('bed_y_mm') or defaults[1]
    boxes = [facts.get('bbox')] + [bounds(obj.get('bbox')) for obj in facts.get('objects', []) if isinstance(obj, dict)]
    for box in boxes:
        if box and ((width and (box[0] < 0 or box[2] > width)) or (depth and (box[1] < 0 or box[3] > depth))):
            return f'Plate geometry exceeds the {width} x {depth} mm bed'
    if model not in ('H2D', 'H2D Pro'):
        return None
    used = {r['extruder'] for r in facts['requirements']}
    # An object can use multiple filaments. Its entire bbox must be reachable
    # by each assigned nozzle. Without object assignments, use the plate bbox.
    checks = []
    for obj in facts.get('objects', []):
        if not isinstance(obj, dict):
            continue
        box = bounds(obj.get('bbox'))
        filaments = obj.get('filament_ids', [])
        targets = {facts['filament_map'].get(i) for i in filaments} - {None}
        if box and targets:
            checks.extend((target, box) for target in targets)
    if facts.get('bbox') and len(used) == 1:
        checks.extend((target, facts['bbox']) for target in used)
    elif len(used) > 1 and not checks:
        note = 'Multiple nozzles used; per-nozzle reach unverified without object assignments'
        if note not in facts['notes']:
            facts['notes'].append(note)
    for target, box in checks:
        low, high, side = (25, 350, 'right') if target == 0 else (0, 325, 'left')
        if box[0] < low or box[2] > high:
            return f'{side} nozzle geometry exceeds X {low}–{high} mm reach'
    return None
