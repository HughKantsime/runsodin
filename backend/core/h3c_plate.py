"""Verify Studio's nine-tool rack layout and source-selecting G-code."""
import math
import re


class RackPlateError(ValueError):
    pass


class RackPlate:
    def __init__(self, settings, plate, used):
        if settings.get('extruder_nozzle_count') not in (['9'], [9]):
            raise RackPlateError('Slice the H3C plate for nine tools. ODIN cannot swap filaments on one tool.')
        nozzles = {}
        for row in plate.findall('nozzle'):
            group = row.get('id', '')
            if not re.fullmatch('[0-8]', group) or group in nozzles:
                raise RackPlateError('The plate has an invalid or repeated nozzle number.')
            nozzles[group] = row
        self.tools = {}
        group_owners = {}
        for index, filament in used.items():
            group = filament.get('group_id')
            nozzle = nozzles.get(group)
            if nozzle is None:
                raise RackPlateError(f'Filament {index + 1} has no matching nozzle in the slice.')
            if group in group_owners:
                raise RackPlateError(f'Filaments {group_owners[group] + 1} and {index + 1} share rack position '
                                     f'{int(group) + 1}. ODIN cannot swap filaments on one tool.')
            group_owners[group] = index
            volume = filament.get('volume_type')
            try:
                diameter = float(filament.get('nozzle_diameter'))
                matches = diameter == float(nozzle.get('nozzle_diameter'))
            except (ValueError, TypeError):
                matches = False
            if not matches or not math.isfinite(diameter) or diameter <= 0:
                raise RackPlateError(f'Check the sliced nozzle diameter for filament {index + 1}.')
            if volume not in ('TPU285', 'Standard') or nozzle.get('volume_type') != volume:
                raise RackPlateError(f'Check the sliced tool type for filament {index + 1}.')
            self.tools[index] = (diameter, volume, int(group))
        source = '(?:' + '|'.join(map(str, used)) + ')'
        number = r'-?[0-9]+(?:\.[0-9]+)?'
        self.auxiliary = re.compile(
            rf'M620 (?:M|[OQ][0-9]+|T{source})|M620\.29 P|M620\.35 S1'
            rf'|M620\.10 (?:A[01] F{number}(?: L{number})? H{number} T{number} P{number} S[01](?: V{number})?|R{number})'
            rf'|M620\.11 (?:P0 I{source} E{number}|K0 I{source} R{number})'
            rf'|M620\.6 I{source} H-1 W[01]|M620\.15 C{number}')
        self.seen = {'T': set(), 'M620': set(), 'M621': set()}
        self.headers = {}
        self.active_source = None

    def check(self, line, command):
        if line.startswith(';'):
            header = re.fullmatch(r';\s*(printer_model|extruder_nozzle_count)\s*=\s*(.*)', line)
            if header:
                key, value = header.groups()
                if key in self.headers:
                    raise RackPlateError('The H3C G-code repeats its printer settings.')
                self.headers[key] = value.strip()
        if not command.startswith(('T', 'M620', 'M621')):
            return
        # Studio's startup "get hotend map" query is also present on one-filament files.
        if command == 'M620 T3' and not self.seen['M620']:
            return
        if self.active_source is None and command in ('M620 S65535', 'T65535', 'M621 S65535'):
            return
        if self.auxiliary.fullmatch(command):
            return
        match = re.fullmatch(r'(T)([0-9]+)|(M62[01]) S([0-9]+)A', command)
        if match:
            kind, index = match.groups()[:2] if match[1] else match.groups()[2:]
            index = int(index)
            if index in self.tools:
                if kind == 'M620' and self.active_source is None:
                    self.active_source = index
                elif kind == 'M621' and self.active_source == index:
                    self.active_source = None
                elif kind != 'T' or self.active_source != index:
                    raise RackPlateError('The H3C tool change disagrees with its M620/M621 block.')
                self.seen[kind].add(index)
                return
        raise RackPlateError('The H3C G-code contains an unsupported tool change.')

    def finish(self):
        if (self.active_source is not None or self.headers.get('printer_model') not in ('H3C', 'Bambu Lab H3C')
                or self.headers.get('extruder_nozzle_count') != '9'
                or any(indices != self.tools.keys() for indices in self.seen.values())):
            raise RackPlateError('The H3C G-code and the sliced tool assignments disagree.')


class RackMessage(str):
    """A plain dispatch refusal with its machine-readable timeline code."""
    def __new__(cls, code, detail):
        message = super().__new__(cls, detail)
        message.code = code
        return message

    def __getnewargs__(self):
        return self.code, str(self)


REQUIRE_VERIFIED_H3C_POSITION = True
POSITION_GATE_MESSAGE = ('Sending an H3C filament from a different position than the slice planned is not yet '
                         'verified on this ODIN. An administrator can enable it in Settings.')


def different_positions_enabled(db):
    from sqlalchemy import text
    from core.system_config_values import config_bool
    return not REQUIRE_VERIFIED_H3C_POSITION or config_bool(db.execute(text(
        "SELECT value FROM system_config WHERE key='h3c_allow_different_positions'")).scalar_one_or_none())


def position_gate(db, sources, mapping):
    if any(mapping[s['source_index']] != s['group_id'] for s in sources):
        if not different_positions_enabled(db):
            return RackMessage('h3c_position_unverified', POSITION_GATE_MESSAGE)
    return None
