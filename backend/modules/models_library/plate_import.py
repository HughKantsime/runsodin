"""Bounded sliced-plate staging and compatibility, without printer I/O."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import math
import re
import stat

from defusedxml import ElementTree as ET
from fastapi import HTTPException

MAX_UPLOAD_BYTES = 100 * 2**20
MAX_PLATE_BYTES = 64 * 2**20
MAX_EXTRACTED_BYTES = 200 * 2**20
MAX_PLATES = 32
MAX_METADATA_BYTES = 4 * 2**20
_PLATE = re.compile(r"Metadata/plate_([1-9][0-9]*)\.gcode")


def plate_filename(original, number):
    from core.display_names import filename_stem
    basename = str(original or 'project.3mf').replace('\\', '/').rsplit('/', 1)[-1]
    stem = re.sub(r'[^A-Za-z0-9._-]', '_', filename_stem(basename))[:120] or 'project'
    # Fixed width prevents plate 1 from matching plate 10 in remote-name matching.
    return f'{stem}_plate_{number:09d}.gcode'


def _number(value, *, integer=False):
    if value is None or value == '':
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 10**8:
        raise ValueError('Invalid plate metadata number')
    if integer and not parsed.is_integer():
        raise ValueError('Invalid plate metadata integer')
    return int(parsed) if integer else parsed


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON metadata key')
        result[key] = value
    return result


def _read_json(package, member):
    value = json.loads(package.read(member), object_pairs_hook=_unique_json_object)
    if type(value) is not dict:
        raise ValueError('Metadata must be a JSON object')
    return value


_MACHINE_KEYS = ('printer_model', 'printer_model_id', 'machine_model')


def _machine_identity(machine):
    """Keep known aliases; unfamiliar models match only case and spacing."""
    from core.printer_identity import canonical_machine
    if machine is None or machine == '':
        return None
    if not isinstance(machine, str):
        raise ValueError('Invalid machine declaration')
    normalized = re.sub(r'\s+', '', machine).casefold()
    if not normalized:
        raise ValueError('Invalid machine declaration')
    return canonical_machine(machine) or normalized


def _nozzle_declarations(data):
    values = []
    for key in ('nozzle_diameter', 'nozzle_diameters'):
        raw = data.get(key)
        if raw is None or raw == '':
            continue
        for value in raw if isinstance(raw, list) else str(raw).split(','):
            values.append(_number(value))
    if values and any(value is None or not 0 < value <= 100 or abs(value - values[0]) > 0.01 for value in values):
        raise ValueError('Conflicting nozzle declarations')
    return values[0] if values else None


def _slice_metadata(package):
    """Read each sparse plate's own slice_info entry. Never reuse plate 1."""
    member = next((name for name in package.namelist() if name.lower() == 'metadata/slice_info.config'), None)
    if member is None:
        return {}
    with package.open(member) as source:
        root = ET.parse(source).getroot()
    result = {}
    for plate in root.findall('plate'):
        meta = {}
        for node in plate.findall('metadata'):
            key = node.get('key')
            if key in meta:
                raise ValueError('Duplicate slice metadata key')
            meta[key] = node.get('value')
        raw = meta.get('index', '')
        if not re.fullmatch(r'[1-9][0-9]{0,8}', raw):
            raise ValueError('Invalid slice plate index')
        number = int(raw)
        if number in result:
            raise ValueError('Ambiguous slice plate index')
        filaments = []
        for node in plate.findall('filament'):
            filaments.append({
                'slot': int(node.get('id', '1')), 'type': node.get('type'),
                'color': node.get('color'), 'used_grams': _number(node.get('used_g')),
                'used_meters': _number(node.get('used_m')),
            })
        grams = _number(meta.get('weight'))
        if filaments and all(f['used_grams'] is not None for f in filaments):
            grams = sum(f['used_grams'] for f in filaments)
        result[number] = {'source_member': member, 'filaments': filaments, 'total_weight_grams': grams,
                          'print_time_seconds': _number(meta.get('prediction'), integer=True),
                          'machines': [meta[k] for k in _MACHINE_KEYS if meta.get(k)],
                          'machine': next((meta[k] for k in _MACHINE_KEYS if meta.get(k)), None),
                          'nozzle': _nozzle_declarations(meta)}
    return result


def extract_to(path, destination):
    """Child-only archive parsing. Member names never become filesystem paths."""
    from core.print_payload import bambu_plate_numbers
    from core.zip_bounds import open_bounded, check_expansion
    from modules.models_library.print_file_meta import extract_print_file_meta, _api_types_for_machine, _fact, _missing_fact
    numbers = bambu_plate_numbers(path)
    if not numbers or len(numbers) > MAX_PLATES or any(n > 999999999 for n in numbers):
        raise ValueError('Import requires 1 to 32 sliced plates')
    with open_bounded(path) as package:
        check_expansion(package)
        seen = set()
        for entry in package.infolist():
            name = entry.filename
            parts = PurePosixPath(name).parts
            if ('\\' in name or name.startswith('/') or '..' in parts
                    or any(':' in part for part in parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1):
                raise ValueError('Unsafe archive member')
            canonical = str(PurePosixPath(name))
            if not entry.is_dir() and (name != canonical or name.casefold() in seen):
                raise ValueError('Ambiguous archive member')
            seen.add(name.casefold())
            if name.lower().endswith(('.config', '.json')) and entry.file_size > MAX_METADATA_BYTES:
                raise ValueError('Archive metadata exceeds 4 MiB')
            # Near-matches are refused rather than silently dropping a plate.
            if re.fullmatch(r'Metadata/plate_.*\.gcode', name, re.IGNORECASE) and not _PLATE.fullmatch(name):
                raise ValueError('Noncanonical plate member')
        members = [package.getinfo(f'Metadata/plate_{n}.gcode') for n in numbers]
        if (any(not 0 < m.file_size <= MAX_PLATE_BYTES for m in members)
                or sum(m.file_size for m in members) > MAX_EXTRACTED_BYTES):
            raise ValueError('Sliced plate size limit exceeded')
        per_plate = _slice_metadata(package)
        project_member = next((name for name in package.namelist() if name.lower() == 'metadata/project_settings.config'), None)
        project = _read_json(package, project_member) if project_member else {}
        _nozzle_declarations(project)
        # Reject ambiguity before the legacy parent parser can apply JSON's
        # last-key-wins behavior to a recognized machine or nozzle field.
        for name in package.namelist():
            if re.fullmatch(r'Metadata/plate_[1-9][0-9]*\.json', name, re.IGNORECASE):
                _nozzle_declarations(_read_json(package, name))
        parent = extract_print_file_meta(path, '.3mf')
        # Refuse every recorded Bambu machine, including a conflicting slice
        # declaration hidden by the parent parser's project-settings precedence.
        machines = [parent['safety_facts']['machine'].get('value')]
        machines += [machine for item in per_plate.values() for machine in item['machines']]
        if project_member:
            machines += [project.get(key) for key in _MACHINE_KEYS]

        project_machines = [project.get(key) for key in _MACHINE_KEYS] if project_member else []
        if len({_machine_identity(machine) for machine in project_machines if machine}) > 1:
            raise ValueError('Conflicting project machine facts')
        if any('bambu' in _api_types_for_machine(machine) for machine in machines):
            raise ValueError('Bambu-sliced plates must remain in their original 3MF')
        # Slice entries and plate JSON describe one plate, not the project.
        # Their fallback facts must never leak into another sparse plate.
        for key in ('machine', 'nozzle'):
            member = parent['safety_facts'][key].get('source_member') or ''
            if member.lower() != 'metadata/project_settings.config':
                parent['safety_facts'][key] = _missing_fact()
        result, total = [], 0
        for number, member in zip(numbers, members):
            metadata = per_plate.get(number, {'filaments': [], 'total_weight_grams': None,
                                             'print_time_seconds': None, 'machine': None, 'nozzle': None})
            facts = json.loads(json.dumps(parent['safety_facts']))
            plate_machines = metadata.get('machines', [])
            if len({_machine_identity(machine) for machine in plate_machines if machine}) > 1:
                raise ValueError('Conflicting plate machine facts')
            source_machine = facts['machine'].get('value')
            if source_machine and any(_machine_identity(machine) != _machine_identity(source_machine) for machine in metadata.get('machines', []) if machine):
                raise ValueError('Conflicting project and plate machine facts')
            source_nozzle = facts['nozzle'].get('value')
            if source_nozzle is not None and metadata['nozzle'] is not None and abs(float(source_nozzle) - metadata['nozzle']) > 0.01:
                raise ValueError('Conflicting project and plate nozzle facts')
            for key in ('machine', 'nozzle'):
                if metadata[key] is not None:
                    facts[key] = _fact(metadata[key], metadata['source_member'], f'plate[{number}].{key}')
            plate_json = next((name for name in package.namelist() if name.lower() == f'metadata/plate_{number}.json'), None)
            if plate_json is not None:
                data = _read_json(package, plate_json)
                json_machines = [(key, data[key]) for key in _MACHINE_KEYS if data.get(key)]
                if len({_machine_identity(machine) for _key, machine in json_machines}) > 1:
                    raise ValueError('Conflicting plate machine facts')
                if json_machines:
                    key, machine = json_machines[0]
                    existing = facts['machine'].get('value')
                    if existing and _machine_identity(existing) != _machine_identity(machine):
                        raise ValueError('Conflicting plate machine facts')
                    if 'bambu' in _api_types_for_machine(machine):
                        raise ValueError('Bambu-sliced plates must remain in their original 3MF')
                    if not existing:
                        facts['machine'] = _fact(machine, plate_json, key)
                nozzle = _nozzle_declarations(data)
                if nozzle is not None:
                    existing = facts['nozzle'].get('value')
                    if existing is not None and abs(float(existing) - nozzle) > 0.01:
                        raise ValueError('Conflicting plate nozzle facts')
                    if not facts['nozzle']['present']:
                        facts['nozzle'] = _fact(nozzle, plate_json, 'nozzle_diameter')
            materials = [f['type'] for f in metadata['filaments'] if f['type']]
            facts['materials'] = (_fact(materials, metadata['source_member'], f'plate[{number}].filament[].type')
                                  if materials else _missing_fact())
            # Every extracted plate keeps parent machine, bed and nozzle provenance.
            meta = {**parent, 'safety_facts': facts}
            generated = Path(destination) / f'{number:09d}.gcode'
            size = 0
            with package.open(member) as source, generated.open('xb') as output:
                while chunk := source.read(65536):
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_PLATE_BYTES or size > member.file_size or total > MAX_EXTRACTED_BYTES:
                        raise ValueError('Plate stream exceeded its size limit')
                    output.write(chunk)
            if size != member.file_size:
                raise ValueError('Incomplete plate stream')
            result.append({'number': number, 'size': size, 'meta': meta,
                           **{k: metadata[k] for k in ('filaments', 'total_weight_grams', 'print_time_seconds')}})
        return {'status': 'ok', 'plates': result}


def validate_result(payload):
    """Schema-check the child manifest before using its numbers or metadata."""
    from modules.models_library.mesh_isolation import _file_meta, _integer, _number as valid_number
    if type(payload) is not dict or set(payload) != {'status', 'plates'} or payload['status'] != 'ok':
        raise ValueError('Invalid plate worker result')
    plates = payload['plates']
    if type(plates) is not list or not 1 <= len(plates) <= MAX_PLATES:
        raise ValueError('Invalid plate count')
    previous, total = 0, 0
    for item in plates:
        if type(item) is not dict or set(item) != {'number', 'size', 'meta', 'filaments', 'total_weight_grams', 'print_time_seconds'}:
            raise ValueError('Invalid plate worker entry')
        if not _integer(item['number'], previous + 1, 999999999) or not _integer(item['size'], 1, MAX_PLATE_BYTES):
            raise ValueError('Invalid plate worker bounds')
        previous = item['number']
        total += item['size']
        if total > MAX_EXTRACTED_BYTES or not _file_meta(item['meta']):
            raise ValueError('Invalid plate worker metadata')
        if not valid_number(item['total_weight_grams'], 0, 1e8, optional=True):
            raise ValueError('Invalid plate grams')
        if item['print_time_seconds'] is not None and not _integer(item['print_time_seconds'], 0, 10**8):
            raise ValueError('Invalid plate duration')
        facts = item['meta']['safety_facts']
        if set(facts) != {'api_types', 'machine', 'bed', 'nozzle', 'materials'}:
            raise ValueError('Invalid plate safety facts')
        if any(type(fact) is not dict or set(fact) != {'present', 'recognized', 'source_member', 'source_key', 'value'}
               for fact in facts.values()):
            raise ValueError('Invalid plate safety fact')
        machine = facts['machine']['value']
        if machine is not None and (type(machine) is not str or len(machine) > 256):
            raise ValueError('Invalid machine fact')
        if not valid_number(facts['nozzle']['value'], 0, 100, optional=True):
            raise ValueError('Invalid nozzle fact')
        filaments = item['filaments']
        if type(filaments) is not list or len(filaments) > 64:
            raise ValueError('Invalid plate filaments')
        seen_slots = set()
        for filament in filaments:
            if type(filament) is not dict or set(filament) != {'slot', 'type', 'color', 'used_grams', 'used_meters'}:
                raise ValueError('Invalid plate filament')
            if not _integer(filament['slot'], 1, 64) or filament['slot'] in seen_slots:
                raise ValueError('Invalid filament slot')
            seen_slots.add(filament['slot'])
            if any(not valid_number(filament[k], 0, 1e8, optional=True) for k in ('used_grams', 'used_meters')):
                raise ValueError('Invalid filament usage')
            if any(v is not None and (type(v) is not str or len(v) > 256) for v in (filament['type'], filament['color'])):
                raise ValueError('Invalid filament labels')
        # JSON can contain escaped lone surrogates that would break responses.
        json.dumps(item, ensure_ascii=False).encode('utf-8')
    return plates


def prepare_staged(analysis, destination):
    """Check generated files and hash all bytes before acquiring the upload lock."""
    if analysis.status != 'ok' or not analysis.imported_plates:
        raise HTTPException(400, 'Unable to import sliced plates. Check archive limits and machine metadata.')
    for item in analysis.imported_plates:
        path = Path(destination) / f"{item['number']:09d}.gcode"
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size != item['size']:
            raise HTTPException(400, 'Invalid staged plate payload')
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while chunk := source.read(65536):
                digest.update(chunk)
        item['sha256'] = digest.hexdigest()
        item['path'] = path
    return analysis.imported_plates


def check_target(plates, target):
    """Only explicit active non-Bambu dispatch targets, with recorded facts intact."""
    from modules.models_library.print_file_meta import _api_types_for_machine
    if (not target.is_active or target.api_type not in ('moonraker', 'prusalink')
            or 'bambu' in _api_types_for_machine(target.machine_type)):
        raise HTTPException(400, 'Plate import requires an active non-Bambu Moonraker or PrusaLink target')
    try:
        target_machine = _machine_identity(target.machine_type)
    except ValueError as exc:
        raise HTTPException(400, 'Selected printer has an invalid machine identity') from exc
    for item in plates:
        facts = item['meta']['safety_facts']
        machine = facts['machine'].get('value')
        if 'bambu' in _api_types_for_machine(machine):
            raise HTTPException(400, 'Bambu-sliced plates cannot be imported as generic G-code')
        source_machine = _machine_identity(machine)
        if source_machine is not None and source_machine != target_machine:
            raise HTTPException(400, 'Plate machine does not match the selected printer')
        x, y = item['meta']['bed_x_mm'], item['meta']['bed_y_mm']
        if x is not None and y is not None:
            if target.bed_x_mm is None or target.bed_y_mm is None or x > target.bed_x_mm + 2 or y > target.bed_y_mm + 2:
                raise HTTPException(400, 'Plate bed exceeds or cannot be checked against the selected printer bed')
        nozzle = facts['nozzle'].get('value')
        if nozzle is not None and (target.nozzle_diameter is None or abs(float(nozzle) - target.nozzle_diameter) > 0.01):
            raise HTTPException(400, 'Plate nozzle does not match the selected printer nozzle')
        # A single nozzle can support an MMU. Existing target metadata has no
        # validated generic multi-material capability, so do not infer support.
        used = [f for f in item['filaments'] if f['used_grams'] is None or f['used_grams'] > 0 or (f['used_meters'] or 0) > 0]
        if len(used) > 1:
            raise HTTPException(400, 'Multi-material plate import requires validated target capabilities and is not supported yet')
