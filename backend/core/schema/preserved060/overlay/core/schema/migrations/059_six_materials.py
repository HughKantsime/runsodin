"""Add five exact material identities to the shared PostgreSQL filament enum.

SQLite already stores these values as VARCHAR(11). No table or stored row is
rewritten. Catalogue backfill runs separately in application startup.
"""
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import ENUM
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:059-six-materials'
LEGACY = ('empty', 'Unknown', 'PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PA', 'PC',
          'PVA', 'OTHER', 'PLA_SUPPORT', 'PLA_CF', 'PETG_CF', 'NYLON_CF',
          'NYLON_GF', 'PC_ABS', 'PC_CF', 'SUPPORT', 'HIPS', 'PPS', 'PPS_CF')
ADDED = ('ASA_AERO', 'ASA_CF', 'PET_CF', 'PPA_CF', 'TPU_AMS')
CURRENT = LEGACY + ADDED
COLUMNS = (('filament_slots', 'filament_type'), ('jobs', 'filament_type'),
           ('models', 'default_filament_type'), ('print_presets', 'filament_type'))


def _shape(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported filament enum dialect')
    inspector = inspect(connection)
    for table, name in COLUMNS:
        column = next((c for c in inspector.get_columns(table) if c['name'] == name), None)
        if column is None:
            raise MigrationError('Filament type column missing')
        datatype = column['type']
        if connection.dialect.name == 'postgresql':
            if not isinstance(datatype, ENUM) or datatype.name != 'filamenttype':
                raise MigrationError('Filament enum column shape differs')
        elif str(datatype).upper() != 'VARCHAR(11)':
            raise MigrationError('Filament type column shape differs')
    if connection.dialect.name == 'postgresql':
        enums = [e for e in inspector.get_enums(schema='public') if e['name'] == 'filamenttype']
        if len(enums) != 1:
            raise MigrationError('Filament enum missing')
        return tuple(enums[0]['labels'])
    return CURRENT


def apply(connection):
    labels = _shape(connection)
    if labels not in (LEGACY, CURRENT):
        raise MigrationError('Filament enum labels differ')
    if connection.dialect.name == 'postgresql' and labels == LEGACY:
        for label in ADDED:
            connection.exec_driver_sql(f"ALTER TYPE public.filamenttype ADD VALUE '{label}'")
    validate(connection)


def validate(connection):
    if _shape(connection) != CURRENT:
        raise MigrationError('Filament enum labels differ')
