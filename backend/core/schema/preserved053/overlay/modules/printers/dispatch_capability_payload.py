"""Bounded, selected-plate nozzle and geometry inspection for Bambu dispatch."""
import io
import json
import zipfile
from core.ams_source_inspection import PlateExtruders, MAX_GCODE_LINE_BYTES
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


def _settings_lines(package, member):
    """Inspect both 2 MiB end windows, with bounded halos for whole lines.

    Use BytesIO.readline: ZipExtFile.readline(limit) can split at its internal
    buffer edge or return more than limit. Never interpret a line fragment as
    a settings delimiter. Diagnostics omit line numbers because the tail window
    does not establish absolute file line numbers.
    """
    limit = 2 * 1024 * 1024
    size = package.getinfo(member).file_size
    active = seen = False
    lines = []
    inspected_end = 0
    with package.open(member) as stream:
        # ZipExtFile.seek otherwise inflates in 16 MiB chunks. Keep the tail
        # window scan bounded too, including two simultaneous dispatches.
        stream.MAX_SEEK_READ = 1024 * 1024
        for offset in dict.fromkeys((0, max(0, size - limit))):
            end = min(size, offset + limit)
            if end <= inspected_end:
                continue
            start = max(offset, inspected_end)
            read_start = max(0, start - MAX_GCODE_LINE_BYTES - 1)
            stream.seek(read_start)
            data = stream.read(min(size, end + MAX_GCODE_LINE_BYTES + 1) - read_start)
            # Back up to the beginning of the first line intersecting the window.
            relative = start - read_start
            boundary = data.rfind(b'\n', 0, relative) + 1
            if not boundary and read_start:
                raise ValueError('gcode_line_too_long')
            window = io.BytesIO(data)
            window.seek(boundary)
            while read_start + window.tell() < end:
                raw = window.readline(MAX_GCODE_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_GCODE_LINE_BYTES:
                    raise ValueError('gcode_line_too_long')
                inspected_end = read_start + window.tell()
                try:
                    line = raw.decode('utf-8').rstrip('\r\n')
                except UnicodeDecodeError:
                    raise ValueError('gcode_settings_encoding_invalid') from None
                if line.strip() == '; CONFIG_BLOCK_START':
                    if seen:
                        raise ValueError('gcode_settings_block_duplicate')
                    active = seen = True
                if active:
                    lines.append(line)
                    if line.strip() == '; CONFIG_BLOCK_END':
                        active = False
            if active:
                raise ValueError('gcode_settings_block_incomplete')
    if not seen:
        raise ValueError('gcode_settings_block_missing')
    return lines


def _verified_extruders(package, names, project, root, selected, plate_number, model):
    if len(names) != len(package.namelist()):
        raise ValueError('archive_member_case_collision')
    if root is None or selected is None:
        raise ValueError('slice_plate_missing')
    if root.findall('.//plate') != root.findall('plate'):
        raise ValueError('slice_plate_structure_unsupported')
    matches = []
    for candidate in root.findall('plate'):
        indexes = [n.get('value') for n in candidate.findall('metadata') if n.get('key') == 'index']
        if str(plate_number) in indexes or candidate.get('index') == str(plate_number):
            if indexes != [str(plate_number)]:
                raise ValueError('slice_plate_index_invalid')
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError('slice_plate_duplicate')
    filaments = selected.findall('filament')
    ids = [f.get('id', '') for f in filaments]
    if not ids or any(not i.isascii() or not i.isdecimal() or not 1 <= int(i) <= 16 for i in ids):
        raise ValueError('slice_filament_id_invalid')
    used = {int(i) - 1 for i in ids}
    if len(used) != len(ids):
        raise ValueError('slice_filament_duplicate')
    # The target model also establishes the H2D physical-map invariant.
    settings = dict(project)
    proof = PlateExtruders(settings, selected, used)
    if bambu_model_name(model) in ('H2D', 'H2D Pro') and proof.physical != [1, 0]:
        raise ValueError('h2d_physical_extruder_map_unsupported')
    member = names.get(f'metadata/plate_{plate_number}.gcode')
    if member is None:
        raise ValueError('gcode_settings_block_missing')
    for line in _settings_lines(package, member):
        proof.check_header(line)
    proof.finish()
    return proof.extruders


def inspect_payload(path, plate_number, model):
    """Selected-plate metadata proof; AMS command restrictions belong to its gate."""
    if type(plate_number) is not int or plate_number < 1:
        raise ValueError('plate_number_invalid')
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
        selected = root = None
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
        # Preserve HEAD's capability interpretation for rack printers (including
        # H2C). This change concerns only H2D's two fixed extruders.
        models = {bambu_model_name(model), bambu_model_name(project.get('printer_model'))}
        verify_mapping = dual and not models.intersection({'H2C', 'H3C'})
        nozzle_map = values(project.get('filament_nozzle_map'))
        if len(nozzle_map) == 1 < len(values(project.get('filament_type'))):
            nozzle_map = []
        mapping = nozzle_map or values(project.get('filament_map'))
        verified = {}
        mapping_refusal = None
        if verify_mapping:
            try:
                verified = _verified_extruders(package, names, project, root, selected, plate_number, model)
            except ValueError as error:
                mapping_refusal = str(error)
            except Exception:
                # Any other read failure (bad CRC, damaged deflate, unsupported
                # compression or encryption) refuses; it must never escape the gate.
                mapping_refusal = 'gcode_unreadable'
        def assignment(index):
            if not dual:
                return 0, 0
            if not verify_mapping:
                logical = int(at(mapping, index)) - 1
                return logical, int(at(physical, logical))
            if mapping_refusal or index not in verified:
                raise ValueError('source_extruder_unverified')
            extruder = verified[index]
            return [int(value) for value in physical].index(extruder), extruder
        requirements = []
        incomplete = False
        filament_to_physical = {}
        filament_requirements = {}
        # Slice-info contains only filaments actually used on this plate.
        for filament in ([] if selected is None else selected.findall('filament')):
            if not verify_mapping and all(number(filament.get(k)) == 0 for k in ('used_g', 'used_m')):
                continue
            try:
                index = int(filament.get('id')) - 1
                logical, extruder = assignment(index)
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
        if not verify_mapping and isinstance(plate_ids, list):
            requirements = []
            for index in plate_ids:
                try:
                    if type(index) is not int or index < 0:
                        raise ValueError()
                    logical, extruder = assignment(index)
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
        return {'printer_model': project.get('printer_model'), 'notes': [], 'requirements': requirements, 'incomplete': incomplete, 'mapping_refusal': mapping_refusal, 'bbox': bounds(plate.get('bbox_all')),
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
