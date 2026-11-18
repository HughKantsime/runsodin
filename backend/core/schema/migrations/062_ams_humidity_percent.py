"""Store the optional humidity percentage reported by a Bambu AMS."""
from sqlalchemy import inspect

from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:062-ams-humidity-percent'


def _column(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported AMS telemetry dialect')
    return next((c for c in inspect(connection).get_columns('ams_telemetry')
                 if c['name'] == 'humidity_percent'), None)


def apply(connection):
    if _column(connection) is None:
        connection.exec_driver_sql('ALTER TABLE ams_telemetry ADD COLUMN humidity_percent REAL')
    validate(connection)


def validate(connection):
    column = _column(connection)
    if (not column or str(column['type']) != 'REAL' or not column['nullable']
            or column['default'] is not None or column.get('computed') or column.get('identity')):
        raise MigrationError('AMS humidity percentage column shape differs')
