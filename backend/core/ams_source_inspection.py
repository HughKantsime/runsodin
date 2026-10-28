"""Bounded interpretation of the single-source Bambu slice with agreeing material facts.

Explicit mappings require one independently verified file source.
Caller owns safe file acquisition; this helper accepts already acquired bytes.
"""
import io
import json
import re
import zipfile
from dataclasses import dataclass
from defusedxml import ElementTree
from core.material_identity import canonical_reported_material

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_GCODE_BYTES = 96 * 1024 * 1024  # H2D evidence has 75,041,281 uncompressed bytes.
MAX_GCODE_LINE_BYTES = 128 * 1024

# Nozzle/flush/cutter command shapes from the genuine H2D slice. Feeds,
# temperatures and lengths vary with material and profile, so they are any
# number; selectors are 0/1 and filament indices (I, M620.17 L) must be the source.
_NUMBER = r'-?[0-9]+(?:\.[0-9]+)?'
_H2D_NON_SOURCE = (
    rf'M620\.10\s+A[01]\s+F{_NUMBER}\s+H{_NUMBER}\s+T{_NUMBER}\s+P{_NUMBER}\s+S[01]',
    r'M620\.11\s+P[01]\s+I{source}\s+B-1\s+E' + _NUMBER,
    # Long retraction appends a feed when enabled.
    rf'M620\.11\s+K[01]\s+I{{source}}\s+B-1\s+R{_NUMBER}(?:\s+F{_NUMBER})?',
    # Flexible filaments (TPU) omit the retraction/distance pair.
    rf'M620\.11\s+S[01]\s+L[01]\s+I{{source}}\s+B-1(?:\s+R{_NUMBER}\s+D{_NUMBER})?\s+E{_NUMBER}\s+F{_NUMBER}',
    # T is the nozzle; L is that nozzle's filament index, or the template's
    # fallback 0 for a nozzle with no filament on a single-source plate.
    rf'M620\.17\s+T[01]\s+S{_NUMBER}\s+L(?:{{source}}|0)',
    r'M620\s+D-1',
    r'M620\.6\s+I{source}\s+H-1\s+W[01]',
    r'M620\s+Q1',
)


@dataclass(frozen=True)
class VerifiedSingleSource:
    plate_index: int
    source_index: int
    material: str


def inspect_single_source_ams(payload: bytes, plate_index: int = 0) -> VerifiedSingleSource:
    """Refuse ambiguous/unsupported sources; never infer metadata array order."""
    refusal = 'Unverifiable source mapping'
    try:
        if type(payload) is not bytes or len(payload) > MAX_ARCHIVE_BYTES:
            raise ValueError(refusal)
        if type(plate_index) is not int or plate_index != 0:
            raise ValueError(refusal)
        # The member count is checked from the end record before the directory is listed.
        from core.zip_bounds import open_bounded
        with open_bounded(io.BytesIO(payload), max_members=512) as package:
            entries = package.infolist()
            if len(entries) > 512 or len({e.filename for e in entries}) != len(entries):
                raise ValueError(refusal)
            plate_members = [e.filename for e in entries
                             if re.fullmatch(r'Metadata/plate_[0-9]+\.gcode', e.filename)]
            if plate_members != ['Metadata/plate_1.gcode']:
                raise ValueError(refusal)
            def read(name, limit):
                member = package.getinfo(name)
                if member.is_dir() or not 0 < member.file_size <= limit or member.compress_size > limit:
                    raise ValueError(refusal)
                with package.open(member) as stream:
                    result = stream.read(limit + 1)
                if len(result) > limit:
                    raise ValueError(refusal)
                return result
            xml = ElementTree.fromstring(read('Metadata/slice_info.config', MAX_XML_BYTES))
            plates = xml.findall('plate')
            if len(plates) != 1:
                raise ValueError(refusal)
            indexes = [m.get('value') for m in plates[0].findall('metadata') if m.get('key') == 'index']
            if indexes != ['1']:
                raise ValueError(refusal)
            filaments = plates[0].findall('filament')
            if len(filaments) != 1 or not re.fullmatch(r'[1-9]|1[0-6]', filaments[0].get('id', '')):
                raise ValueError(refusal)
            source_index = int(filaments[0].get('id')) - 1
            if 'Metadata/plate_1.json' in package.namelist():
                plate = json.loads(read('Metadata/plate_1.json', MAX_XML_BYTES))
                # Older slicers omit filament_ids; the plate XML and command
                # scan still verify the source. A present value must agree.
                if not isinstance(plate, dict):
                    raise ValueError(refusal)
                if 'filament_ids' in plate:
                    ids = plate['filament_ids']
                    if type(ids) is not list or len(ids) != 1 or type(ids[0]) is not int or ids != [source_index]:
                        raise ValueError(refusal)
            material = canonical_reported_material(filaments[0].get('type'))
            settings = json.loads(read('Metadata/project_settings.config', MAX_XML_BYTES))
            materials = settings.get('filament_type') if isinstance(settings, dict) else None
            # Saved profiles are not evidence of used sources. The plate XML
            # and command scan independently require exactly the declared source.
            # Check its corresponding profile without inventing unused trays.
            if material is None or not isinstance(materials, list) or not source_index < len(materials) <= 16 or canonical_reported_material(materials[source_index]) != material:
                raise ValueError(refusal)
            code = io.BytesIO(read('Metadata/plate_1.gcode', MAX_GCODE_BYTES))
            headers = []
            switches = {620: set(), 621: set()}
            for raw_line in iter(lambda: code.readline(MAX_GCODE_LINE_BYTES + 1), b''):
                if len(raw_line) > MAX_GCODE_LINE_BYTES:
                    raise ValueError(refusal)
                line = raw_line.decode('utf-8')
                line = line[:-2] if line.endswith('\r\n') else line.removesuffix('\n')
                if any(separator in line for separator in ('\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')):
                    raise ValueError(refusal)
                header = re.fullmatch(r'\s*;\s*filament_type\s*=\s*(.*?)\s*', line)
                if header:
                    header_materials = header.group(1).split(';')
                    if not 1 <= len(header_materials) <= 16:
                        raise ValueError(refusal)
                    headers.append([canonical_reported_material(value.strip()) for value in header_materials])
                command = line.split(';', 1)[0].strip()
                if re.match(r'^T(?:[0-9]|\s|$)', command):
                    # Stock X1 calibration uses T1100, then T1000 with the
                    # comment "change to nozzle space": a template sentinel,
                    # rather than evidence of a numbered AMS source.
                    # H2D uses T1001 in its start template, T65535 to pull
                    # filament back to AMS, and T65279 in its end template.
                    # These are non-source sentinels, never numbered trays.
                    if not re.fullmatch(rf'T(?:{source_index}(?:\s+H-1)?|255|1000|1001|1100|65535|65279)', command):
                        raise ValueError(refusal)
                if not re.match(r'^M62[01](?:\s|\.|$)', command):
                    continue
                # Known non-source remap/tangle commands in this genuine slice.
                if re.fullmatch(r'M620\s+M|M620\.3\s+W[01]|M620\.1\s+E\s+F[0-9]+(?:\.[0-9]+)?\s+T[0-9]+(?:\.[0-9]+)?', command):
                    continue
                if any(re.fullmatch(pattern.replace('{source}', str(source_index)), command) for pattern in _H2D_NON_SOURCE):
                    continue
                if re.fullmatch(r'M62[01]\s+S255A?', command):
                    continue
                # H2D end-template sentinels pair with T65535/T65279 above.
                if re.fullmatch(r'M62[01]\s+S(?:65535|65279)', command):
                    continue
                match = re.fullmatch(r'M(620|621)\s+S(0|[1-9][0-9]*)A', command)
                if match is None:
                    match = re.fullmatch(r'M(620)\s+S(0|[1-9][0-9]*)A\s+H-1', command)
                if match is None:
                    raise ValueError(refusal)
                switches[int(match.group(1))].add(int(match.group(2)))
            expected_materials = [canonical_reported_material(value) for value in materials]
            if (any(value is None for value in expected_materials) or headers != [expected_materials]
                    or switches != {620: {source_index}, 621: {source_index}}):
                raise ValueError(refusal)
        return VerifiedSingleSource(0, source_index, material)
    except Exception:
        raise ValueError(refusal) from None
