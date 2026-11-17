"""Nullable customer catalogue metadata and a partial identity index."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:058-filament-catalogue'
COLUMNS = {'catalogue_type': 'VARCHAR(100)', 'catalogue_code': 'VARCHAR(100)', 'catalogue_colors': 'JSON'}
INDEX = 'uq_filament_catalogue_code'


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported catalogue dialect')
    existing = {c['name'] for c in inspect(connection).get_columns('filament_library')}
    for name, kind in COLUMNS.items():
        if name not in existing:
            connection.exec_driver_sql(f'ALTER TABLE filament_library ADD COLUMN {name} {kind}')
    connection.exec_driver_sql(f'CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} '
                               'ON filament_library (brand, catalogue_code) WHERE catalogue_code IS NOT NULL')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    columns = {c['name']: c for c in inspector.get_columns('filament_library')}
    for name, kind in COLUMNS.items():
        column = columns.get(name)
        if (column is None or str(column['type']).upper() != kind or not column['nullable']
                or column.get('default') is not None):
            raise MigrationError('Filament catalogue column shape differs')
    index = next((i for i in inspector.get_indexes('filament_library') if i['name'] == INDEX), None)
    predicate = str((index or {}).get('dialect_options', {}).get(connection.dialect.name + '_where', ''))
    predicate = ''.join(predicate.lower().replace('(', '').replace(')', '').split())
    if not index or not index['unique'] or index['column_names'] != ['brand', 'catalogue_code'] or predicate != 'catalogue_codeisnotnull':
        raise MigrationError('Filament catalogue index shape differs')

