"""Operator feed exclusions and credential-free ACE status; additive only."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:057-slot-feed-policy'
COLUMNS = {'filament_slots': {'do_not_feed': ('BOOLEAN', False, 'false'),
                             'do_not_feed_note': ('VARCHAR(200)', True, None)},
           'printers': {'ace_status': ('TEXT', True, None)}}


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported feed policy dialect')
    for table, columns in COLUMNS.items():
        existing = {c['name'] for c in inspect(connection).get_columns(table)}
        for name, (kind, nullable, default) in columns.items():
            if name not in existing:
                value = '0' if connection.dialect.name == 'sqlite' else 'false'
                suffix = '' if nullable else ' NOT NULL DEFAULT ' + value
                connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} {kind}{suffix}')
    validate(connection)


def validate(connection):
    for table, columns in COLUMNS.items():
        actual = {c['name']: c for c in inspect(connection).get_columns(table)}
        for name, (kind, nullable, default) in columns.items():
            if default == 'false' and connection.dialect.name == 'sqlite':
                default = '0'
            c = actual.get(name)
            if not c or str(c['type']).upper() != kind or c['nullable'] != nullable or (str(c.get('default')).lower() if c.get('default') is not None else None) != default:
                raise MigrationError('Slot feed policy shape differs')
