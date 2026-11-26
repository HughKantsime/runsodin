"""Track the last time step accepted for each user's authenticator."""
from sqlalchemy import inspect

from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:065-mfa-last-totp-step'


def _column(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported MFA time step dialect')
    return next((c for c in inspect(connection).get_columns('users')
                 if c['name'] == 'mfa_last_totp_step'), None)


def apply(connection):
    if _column(connection) is None:
        connection.exec_driver_sql('ALTER TABLE users ADD COLUMN mfa_last_totp_step INTEGER')
    validate(connection)


def validate(connection):
    column = _column(connection)
    if (not column or str(column['type']) != 'INTEGER' or not column['nullable']
            or column['default'] is not None or column.get('computed') or column.get('identity')):
        raise MigrationError('MFA time step column shape differs')
