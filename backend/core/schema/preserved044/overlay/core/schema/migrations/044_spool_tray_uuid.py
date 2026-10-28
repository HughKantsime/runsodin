"""Spool-level Bambu identity, learned only when observed."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:044-spool-tray-uuid'


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported spool identity dialect')
    columns = {c['name'] for c in inspect(connection).get_columns('spools')}
    if 'tray_uuid' not in columns:
        connection.exec_driver_sql('ALTER TABLE spools ADD COLUMN tray_uuid VARCHAR(32)')
        connection.exec_driver_sql('CREATE UNIQUE INDEX ix_spools_tray_uuid ON spools (tray_uuid)')
    if 'remaining_measured_at' not in columns:
        connection.exec_driver_sql('ALTER TABLE spools ADD COLUMN remaining_measured_at TIMESTAMP')
    if 'remaining_measured_g' not in columns:
        connection.exec_driver_sql('ALTER TABLE spools ADD COLUMN remaining_measured_g FLOAT')
    if 'remaining_measured_printer_id' not in columns:
        connection.exec_driver_sql('ALTER TABLE spools ADD COLUMN remaining_measured_printer_id INTEGER')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    column = next((c for c in inspector.get_columns('spools') if c['name'] == 'tray_uuid'), None)
    if column is None or str(column['type']).upper() != 'VARCHAR(32)' or not column['nullable'] or column.get('default') is not None:
        raise MigrationError('Spool tray UUID shape differs')
    indexes = [i for i in inspector.get_indexes('spools') if i['name'] == 'ix_spools_tray_uuid']
    if len(indexes) != 1 or indexes[0]['column_names'] != ['tray_uuid'] or not indexes[0]['unique']:
        raise MigrationError('Spool tray UUID unique index differs')
    options = indexes[0].get('dialect_options', {})
    if set(options) - {'postgresql_include', 'postgresql_nulls_not_distinct'} or options.get('postgresql_include') or options.get('postgresql_nulls_not_distinct'):
        raise MigrationError('Spool tray UUID index options differ')

    columns = {c['name']: c for c in inspector.get_columns('spools')}
    for name, types in {'remaining_measured_at': {'TIMESTAMP', 'DATETIME'},
                        'remaining_measured_g': {'FLOAT', 'DOUBLE PRECISION'},
                        'remaining_measured_printer_id': {'INTEGER'}}.items():
        column = columns.get(name)
        if column is None or str(column['type']).upper() not in types or not column['nullable'] or column.get('default') is not None:
            raise MigrationError('Spool measurement basis shape differs')
