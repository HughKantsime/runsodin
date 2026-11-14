"""Customer-supplied catalogue data. Type aliases contain no catalogue colour data."""
import json
import re

from sqlalchemy import text
from core.material_identity import canonical_reported_material
from modules.inventory.models import FilamentLibrary

MAX_BYTES = 1024 * 1024
MAX_ENTRIES = 2000
# Hand-written from the profile material facts in the approved specification.
TYPE_GROUPS = {
    'PLA': ('PLA Basic', 'PLA Dynamic', 'PLA Galaxy', 'PLA Glow', 'PLA Lite',
            'PLA Marble', 'PLA Matte', 'PLA Metal', 'PLA Palette', 'PLA Pure',
            'PLA Silk', 'PLA Silk+', 'PLA Sparkle', 'PLA Tough', 'PLA Tough+',
            'PLA Translucent', 'PLA Wood', 'Support for PLA', 'Support for PLA/PETG'),
    'PLA-AERO': ('PLA Aero',), 'PLA-CF': ('PLA-CF',),
    'PETG': ('PETG Basic', 'PETG HF', 'PETG Matte', 'PETG Translucent'),
    'PETG-CF': ('PETG-CF',), 'ABS': ('ABS', 'Support for ABS'),
    'ABS-GF': ('ABS-GF',), 'ASA': ('ASA',), 'PA': ('Support for PA/PET',),
    'PA-CF': ('PAHT-CF',), 'PA-GF': ('PA6-GF',), 'PA6-CF': ('PA6-CF',),
    'PC': ('PC', 'PC FR'), 'PVA': ('PVA',),
    'TPU': ('TPU 75A (2.85mm)', 'TPU 85A', 'TPU 90A', 'TPU 95A', 'TPU 95A HF'),
    'ASA-AERO': ('ASA Aero',), 'ASA-CF': ('ASA-CF',), 'PET-CF': ('PET-CF',),
    'PPA-CF': ('PPA-CF',), 'PPS-CF': ('PPS-CF',), 'TPU-AMS': ('TPU for AMS',),
}
BAMBU_TYPES = {name: material for material, names in TYPE_GROUPS.items() for name in names}
_ALIASES = {name.casefold(): material for name, material in BAMBU_TYPES.items()}


def base_material(name):
    label = _ALIASES.get(name.strip().casefold()) if isinstance(name, str) else None
    return canonical_reported_material(label)


def _label(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid catalogue text')
    value.encode("utf-8", errors="strict")
    return value.strip()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate catalogue field')
        result[key] = value
    return result


def parse_catalogue(raw):
    """Validate the complete bounded upload before touching any stored row."""
    if len(raw) > MAX_BYTES:
        raise ValueError('Choose a catalogue file smaller than 1 MB.')
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_keys,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        entries = payload['data']
        if (not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES
                or type(payload['total']) is not int or payload['total'] != len(entries)):
            raise ValueError()
        result, codes = [], set()
        for entry in entries:
            name = _label(entry['fila_type'], 100)
            color_name = _label(entry['fila_color_name']['en'], 100)
            code = _label(entry['fila_color_code'], 100)
            colors = entry['fila_color']
            if (not isinstance(colors, list) or len(colors) not in (1, 2, 4)
                    or any(not isinstance(c, str) or not re.fullmatch(r'#[0-9a-fA-F]{8}', c) for c in colors)
                    or code in codes):
                raise ValueError()
            codes.add(code)
            result.append(dict(catalogue_type=name, catalogue_code=code,
                               name=f'{name} {color_name}', material=base_material(name) or 'OTHER',
                               color_hex=colors[0][1:7].upper(), catalogue_colors=colors))
        return result
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise ValueError('This file is not a valid Bambu catalogue. Check the file and try again.') from None


def import_catalogue(db, entries):
    """Serialize imports; callers audit and commit the entire operation together."""
    connection = db.connection()
    if connection.dialect.name == 'postgresql':
        connection.execute(text('SELECT pg_advisory_xact_lock(1330071888, 1)'))
    elif connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    created = updated = preserved = 0
    skipped = []
    existing = {row.catalogue_code: row for row in db.query(FilamentLibrary).filter(
        FilamentLibrary.brand == 'Bambu Lab',
        FilamentLibrary.catalogue_code.in_([entry['catalogue_code'] for entry in entries])).all()}
    for entry in entries:
        row = existing.get(entry['catalogue_code'])
        if row is None:
            row = FilamentLibrary(brand='Bambu Lab', is_custom=False, **entry)
            db.add(row)
            created += 1
        else:
            # Never apply a different material's name or colours to this identity.
            # Compared by the same exact-label rule as matching, so a stored spelling such as ' pla ' is not a change.
            if (row.material != entry['material']
                    and (canonical_reported_material(row.material) is None
                         or canonical_reported_material(row.material) != canonical_reported_material(entry['material']))):
                preserved += 1
                skipped.append(dict(catalogue_code=row.catalogue_code, reason='type_changed'))
                continue
            preserved += int(row.catalogue_type != entry['catalogue_type'])
            for field in ('name', 'color_hex', 'catalogue_colors'):
                setattr(row, field, entry[field])
            updated += 1
    db.flush()
    return dict(created=created, updated=updated, preserved=preserved, skipped=skipped,
                unrecognised_types=sorted({e['catalogue_type'] for e in entries if e['material'] == 'OTHER'}))


def catalogue_fields(filament):
    return {field: getattr(filament, field) for field in ('catalogue_type', 'catalogue_code', 'catalogue_colors')}


def catalogue_key(material, color):
    """The same material/RGB identity for display and accepting a choice."""
    material = canonical_reported_material(material)
    if not isinstance(color, str):
        return None
    color = color.lstrip('#').upper()
    if material is None or not re.fullmatch('[0-9A-F]{6}([0-9A-F]{2})?', color):
        return None
    return material, color[:6]


def matching_entries(db, material, color):
    key = catalogue_key(material, color)
    if key is None:
        return []
    entries = db.query(FilamentLibrary).filter(
        FilamentLibrary.catalogue_code.isnot(None)).order_by(FilamentLibrary.id).all()
    return [entry for entry in entries if catalogue_key(entry.material, entry.color_hex) == key]
