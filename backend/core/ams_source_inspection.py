"""Bounded interpretation of Bambu plate sources with agreeing material facts.
Caller owns safe file acquisition; this helper accepts already acquired bytes.
"""
import io
import json
import math
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

# BambuStudio A1/P1S/X1C/H2D change_filament_gcode templates. These
# configure travel, flush and retraction; I still names a verified source.
_MULTI_SOURCE_AUXILIARY = (
    rf'M620\.1\s+X{_NUMBER}\s+Y{_NUMBER}\s+F{_NUMBER}\s+P[012]',
    r'M620\.11\s+S0',
    rf'M620\.11\s+S1\s+I{{source}}\s+E{_NUMBER}\s+F{_NUMBER}',
    rf'M620\.10\s+A0\s+F{_NUMBER}',
    rf'M620\.10\s+A1\s+F{_NUMBER}\s+L{_NUMBER}\s+H{_NUMBER}\s+T{_NUMBER}',
    rf'M620\.10\s+A[01]\s+F{_NUMBER}\s+L{_NUMBER}\s+H{_NUMBER}\s+T{_NUMBER}\s+P{_NUMBER}\s+S[01]',
    rf'M620\.11\s+P1\s+I{{source}}\s+B-1\s+E{_NUMBER}\s+F{_NUMBER}',
    rf'M620\.15\s+C{_NUMBER}',
    r'M620\.11\s+H0|M620\.11\s+H2\s+C331',
    rf'M620\.10\s+R{_NUMBER}',
    rf'M620\.13\s+W0\s+L{_NUMBER}\s+T{_NUMBER}\s+R{_NUMBER}',
    r'M620\s+Q[1-9][0-9]*',
)

# A tool change can name the current hotend (B0 or B1) rather than B-1, which comes
# from the first change or a wipe tower change. B is the filament_map value minus one,
# so it must agree with the file's own map: I names a verified source and B its hotend.
_CROSS_NOZZLE = tuple(re.compile(pattern) for pattern in (
    rf'M620\.11\s+P[01]\s+I([0-9]+)\s+B([01])\s+E{_NUMBER}(?:\s+F{_NUMBER})?',
    rf'M620\.11\s+K[01]\s+I([0-9]+)\s+B([01])\s+R{_NUMBER}(?:\s+F{_NUMBER})?',
    rf'M620\.11\s+S[01]\s+L[01]\s+I([0-9]+)\s+B([01])(?:\s+R{_NUMBER}\s+D{_NUMBER})?\s+E{_NUMBER}\s+F{_NUMBER}',
))

# Bambu Lab P2S: the only forms in Bambu Studio's public profile since its first commit
# (2025/10 to 2026/05, unchanged in 02.08.02.61), and those of a real slice. In M620.11, P is
# long_retraction_when_cut with E its distance and F its feed (P1 is only in the change
# template, so a single-source file has none). K R is the H2D template's long retraction on
# an extruder change, fixed off here. L is 0 throughout and isn't documented. I is
# current_filament_id: the filament being left, or in the start template the one about to
# load. M620.6 I W1 is "enable ams air printing detect" for the filament just loaded.
_P2S_MODEL = 'Bambu Lab P2S'
_P2S_SOURCE = '(0|[1-9][0-9]*)'
_P2S_LEAVING = re.compile(rf'M620\.11 (?:P0 L0 I{_P2S_SOURCE} E0|K0 I{_P2S_SOURCE} R0)')
_P2S_LONG_RETRACTION = re.compile(rf'M620\.11 P1 L0 I{_P2S_SOURCE} E-[0-9]+(?:\.[0-9]+)? F[0-9]+(?:\.[0-9]+)?')
_P2S_AIR_DETECT = re.compile(rf'M620\.6 I{_P2S_SOURCE} W1')
# The rest of the template; the shared checks still decide these.
_P2S_SHARED = re.compile(
    rf'M620 M|M620\.10 A[01] F{_NUMBER}(?: L{_NUMBER})? H{_NUMBER} T{_NUMBER} P{_NUMBER} S1'
    rf'|M620\.10 R{_NUMBER}|M620\.15 C{_NUMBER}')
# machine_end_gcode's "pull back filament to AMS", once, after the slicer's end marker.
# Bambu Studio's parser treats only T0-T254 as a filament. What the firmware does with 65535
# isn't published, so nothing may extrude or change tool afterwards.
_P2S_END_MARKER = '; MACHINE_END_GCODE_START'
_P2S_PULL_BACK = ('M620 S65535', 'T65535', 'M621 S65535')
_TOOL_COMMAND = re.compile(r'T(?:[0-9]|\s|$)|M62[01](?:\s|\.|$)')


class MultipleAMSSources(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedSingleSource:
    plate_index: int
    source_index: int
    material: str


@dataclass(frozen=True)
class VerifiedSource:
    source_index: int
    material: str
    extruder: int | None
    used_grams: float | None


@dataclass(frozen=True)
class VerifiedSources:
    plate_index: int
    sources: tuple[VerifiedSource, ...]


def _map_values(value):
    if isinstance(value, list):
        return value
    return value.replace(',', ' ').split() if isinstance(value, str) else []


def _source_extruder(settings, plate, index):
    physical = _map_values(settings.get('physical_extruder_map'))
    maps = [_map_values(settings[key]) for key in ('filament_nozzle_map', 'filament_map') if key in settings]
    # The slicer CLI leaves filament_nozzle_map at a one-entry default on a
    # multi-filament file: that is not a fact about this filament.
    filaments = settings.get('filament_type')
    if 'filament_nozzle_map' in settings and isinstance(filaments, list) and len(maps[0]) == 1 < len(filaments):
        maps.pop(0)
    declared = [node.get('value') for node in plate.findall('metadata') if node.get('key') == 'filament_maps']
    if len(declared) > 1:
        raise ValueError('Ambiguous plate extruders')
    maps.extend(_map_values(value) for value in declared)
    if not physical or not maps:
        return None
    targets = []
    for mapping in maps:
        if index >= len(mapping):
            return None
        logical = mapping[index]
        if type(logical) not in (int, str) or not re.fullmatch('[12]', str(logical)):
            return None
        logical = int(logical) - 1
        if logical >= len(physical):
            return None
        extruder = physical[logical]
        if type(extruder) not in (int, str) or str(extruder) not in ('0', '1'):
            return None
        targets.append(int(extruder))
    if len(set(targets)) != 1:
        raise ValueError('Conflicting source extruders')
    return targets[0]


def _cross_nozzle_agrees(command, settings, used):
    """None when this isn't a cross-nozzle change; else whether it agrees with the file."""
    match = next(filter(None, (pattern.fullmatch(command) for pattern in _CROSS_NOZZLE)), None)
    if match is None:
        return None
    source, hotend = map(int, match.groups())
    mapping = _map_values(settings.get('filament_map'))
    return (source in used and source < len(mapping)
            and str(mapping[source]) in ('1', '2') and int(mapping[source]) - 1 == hotend)


class _P2STemplate:
    """Holds a file that declares a P2S to that printer's own template, line by line."""

    def __init__(self, used):
        self.used = used
        self.models = []
        self.in_end_template = False
        self.pulled_back = 0
        self.loaded = None  # The source of the latest T.
        self.first_named = set()  # Sources named before the first T, which must load them.

    def admits(self, line, command):
        """True: this command is settled here. False: the shared checks decide it."""
        header = re.fullmatch(r'\s*;\s*printer_model\s*=\s*(.*?)\s*', line)
        if header:
            self.models.append(header.group(1))
        if line.strip() == _P2S_END_MARKER:
            self.in_end_template = True
        if not _TOOL_COMMAND.match(command):
            if self.pulled_back and re.match(r'G0*[0-3](?![0-9])', command) and 'E' in command:
                raise ValueError('P2S extrusion after the pull back')
            return False
        if command in _P2S_PULL_BACK:
            if not self.in_end_template or _P2S_PULL_BACK[self.pulled_back:self.pulled_back + 1] != (command,):
                raise ValueError('P2S pull back out of place')
            self.pulled_back += 1
            return True
        if self.in_end_template:
            raise ValueError('P2S end template has no other tool command')
        tool = re.fullmatch(r'T([0-9]+)', command)
        if tool:
            self.loaded = int(tool.group(1))
            if self.loaded not in self.used or self.first_named - {self.loaded}:
                raise ValueError('P2S tool is not the source the file named')
            self.first_named.clear()
            return False
        switch = re.fullmatch(r'M62[01] S([0-9]+)A', command)
        if switch:
            if int(switch.group(1)) not in self.used:
                raise ValueError('P2S switch to a source the file does not declare')
            return False
        leaving = _P2S_LEAVING.fullmatch(command)
        if leaving:
            source = int(leaving.group(1) or leaving.group(2))
            if self.loaded is None:
                self.first_named.add(source)
            elif source != self.loaded:
                raise ValueError('P2S command names a source that is not loaded')
            return True
        long_retraction = _P2S_LONG_RETRACTION.fullmatch(command)
        air_detect = _P2S_AIR_DETECT.fullmatch(command)
        if long_retraction or air_detect:
            source = int((long_retraction or air_detect).group(1))
            if source != self.loaded or (long_retraction and len(self.used) == 1):
                raise ValueError('P2S command names a source that is not loaded')
            return True
        if not _P2S_SHARED.fullmatch(command):
            raise ValueError('Not a P2S template command')
        return False

    def finish(self):
        if self.first_named or self.pulled_back != len(_P2S_PULL_BACK) or self.models != [_P2S_MODEL]:
            raise ValueError('Incomplete P2S template')


def inspect_single_source_ams(payload: bytes, plate_index: int = 0) -> VerifiedSingleSource:
    verified = inspect_ams_sources(payload, plate_index, single_source=True)
    source = verified.sources[0]
    return VerifiedSingleSource(verified.plate_index, source.source_index, source.material)


def inspect_ams_sources(payload: bytes, plate_index: int = 0, *, single_source=False) -> VerifiedSources:
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
            if (not 1 <= len(filaments) <= 16
                    or any(not re.fullmatch(r'[1-9]|1[0-6]', f.get('id', '')) for f in filaments)):
                raise ValueError(refusal)
            by_source = {int(f.get('id')) - 1: f for f in filaments}
            if len(by_source) != len(filaments):
                raise ValueError(refusal)
            if single_source and len(filaments) != 1:
                raise MultipleAMSSources('AMS read and approval require a single-source file; multi-source files are unsupported')
            used = set(by_source)
            source_pattern = '(?:' + '|'.join(str(i) for i in sorted(used)) + ')'
            if 'Metadata/plate_1.json' in package.namelist():
                plate = json.loads(read('Metadata/plate_1.json', MAX_XML_BYTES))
                # Older slicers omit filament_ids; the plate XML and command
                # scan still verify the source. A present value must agree.
                if not isinstance(plate, dict):
                    raise ValueError(refusal)
                if 'filament_ids' in plate:
                    ids = plate['filament_ids']
                    if (type(ids) is not list or any(type(i) is not int for i in ids)
                            or len(ids) != len(used) or set(ids) != used):
                        raise ValueError(refusal)
            settings = json.loads(read('Metadata/project_settings.config', MAX_XML_BYTES))
            materials = settings.get('filament_type') if isinstance(settings, dict) else None
            if not isinstance(materials, list) or not max(used) < len(materials) <= 16:
                raise ValueError(refusal)
            sources = []
            for index, filament in sorted(by_source.items()):
                material = canonical_reported_material(filament.get('type'))
                if material is None or canonical_reported_material(materials[index]) != material:
                    raise ValueError(refusal)
                grams = filament.get('used_g')
                if grams is not None:
                    grams = float(grams)
                    if not math.isfinite(grams) or grams < 0:
                        raise ValueError(refusal)
                sources.append(VerifiedSource(index, material,
                    None if single_source else _source_extruder(settings, plates[0], index), grams))
            extruders = {source.source_index: source.extruder for source in sources}
            code = io.BytesIO(read('Metadata/plate_1.gcode', MAX_GCODE_BYTES))
            p2s = _P2STemplate(used) if settings.get('printer_model') == _P2S_MODEL else None
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
                if p2s is not None and p2s.admits(line, command):
                    continue
                if re.match(r'^T(?:[0-9]|\s|$)', command):
                    # Stock X1 calibration uses T1100, then T1000 with the
                    # comment "change to nozzle space": a template sentinel,
                    # rather than evidence of a numbered AMS source.
                    # H2D uses T1001 in its start template, T65535 to pull
                    # filament back to AMS, and T65279 in its end template.
                    # These are non-source sentinels, never numbered trays.
                    if not re.fullmatch(rf'T(?:{source_pattern}(?:\s+H-1)?|255|1000|1001|1100|65535|65279)', command):
                        raise ValueError(refusal)
                if not re.match(r'^M62[01](?:\s|\.|$)', command):
                    continue
                # Known non-source remap/tangle commands in this genuine slice.
                if re.fullmatch(r'M620\s+M|M620\.3\s+W[01]|M620\.1\s+E\s+F[0-9]+(?:\.[0-9]+)?\s+T[0-9]+(?:\.[0-9]+)?', command):
                    continue
                nozzle = re.fullmatch(r'M620\.17\s+T([01])\s+S' + _NUMBER + r'\s+L([0-9]+)', command)
                if nozzle and len(used) > 1 and all(value is not None for value in extruders.values()):
                    target, source = map(int, nozzle.groups())
                    if extruders.get(source) != target and not (source == 0 and target not in extruders.values()):
                        raise ValueError(refusal)
                if any(re.fullmatch(pattern.replace('{source}', source_pattern), command) for pattern in _H2D_NON_SOURCE):
                    continue
                if len(used) > 1 and any(re.fullmatch(pattern.replace('{source}', source_pattern), command)
                                         for pattern in _MULTI_SOURCE_AUXILIARY):
                    continue
                if len(used) > 1:
                    crossing = _cross_nozzle_agrees(command, settings, used)
                    if crossing is False:
                        raise ValueError(refusal)
                    if crossing:
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
            if p2s is not None:
                p2s.finish()
            expected_materials = [canonical_reported_material(value) for value in materials]
            if (any(value is None for value in expected_materials) or headers != [expected_materials]
                    or switches != {620: used, 621: used}):
                raise ValueError(refusal)
        return VerifiedSources(0, tuple(sources))
    except MultipleAMSSources:
        raise
    except Exception:
        raise ValueError(refusal) from None
