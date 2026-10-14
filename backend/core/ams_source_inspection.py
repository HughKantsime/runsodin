"""Bounded interpretation of the single-source Bambu slice with agreeing material facts.

Unwired acceptance helper. Other slicing workflows retain existing behavior.
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
MAX_GCODE_BYTES = 32 * 1024 * 1024


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
        with zipfile.ZipFile(io.BytesIO(payload)) as package:
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
            if len(filaments) != 1 or filaments[0].get('id') != '1':
                raise ValueError(refusal)
            material = canonical_reported_material(filaments[0].get('type'))
            settings = json.loads(read('Metadata/project_settings.config', MAX_XML_BYTES))
            materials = settings.get('filament_type') if isinstance(settings, dict) else None
            if material is None or not isinstance(materials, list) or len(materials) != 1 or canonical_reported_material(materials[0]) != material:
                raise ValueError(refusal)
            code = read('Metadata/plate_1.gcode', MAX_GCODE_BYTES).decode('utf-8')
            headers = []
            switches = {620: set(), 621: set()}
            for line in code.splitlines():
                header = re.fullmatch(r'\s*;\s*filament_type\s*=\s*(.*?)\s*', line)
                if header:
                    headers.append(canonical_reported_material(header.group(1)))
                command = line.split(';', 1)[0].strip()
                if re.match(r'^T(?:[0-9]|\s|$)', command):
                    if not re.fullmatch(r'T(?:0|255|1000)', command):
                        raise ValueError(refusal)
                if not re.match(r'^M62[01](?:\s|\.|$)', command):
                    continue
                # Known non-source remap/tangle commands in this genuine slice.
                if re.fullmatch(r'M620\s+M|M620\.3\s+W[01]|M620\.1\s+E\s+F[0-9]+(?:\.[0-9]+)?\s+T[0-9]+(?:\.[0-9]+)?', command):
                    continue
                if re.fullmatch(r'M62[01]\s+S255A?', command):
                    continue
                match = re.fullmatch(r'M(620|621)\s+S(0|[1-9][0-9]*)A', command)
                if match is None:
                    raise ValueError(refusal)
                switches[int(match.group(1))].add(int(match.group(2)))
            if headers != [material] or switches != {620: {0}, 621: {0}}:
                raise ValueError(refusal)
        return VerifiedSingleSource(0, 0, material)
    except Exception:
        raise ValueError(refusal) from None
