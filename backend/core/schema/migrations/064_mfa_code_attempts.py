"""Keep at most one rolling MFA failure window per account."""
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, Text, inspect

from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:064-mfa-code-attempts'
metadata = MetaData()
Table('users', metadata, Column('id', Integer, primary_key=True))
attempts = Table('mfa_code_attempts', metadata,
    Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Column('failure_times', Text, nullable=False))


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported MFA code attempt database')
    attempts.create(connection, checkfirst=True)
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    columns = inspector.get_columns(attempts.name)
    if ({col['name']: (str(col['type']), col['nullable'], col['default']) for col in columns}
            != {'user_id': ('INTEGER', False, None), 'failure_times': ('TEXT', False, None)}
            or inspector.get_pk_constraint(attempts.name)['constrained_columns'] != ['user_id']):
        raise MigrationError('MFA code attempt columns differ')
    keys = inspector.get_foreign_keys(attempts.name)
    if (len(keys) != 1 or keys[0]['constrained_columns'] != ['user_id']
            or keys[0]['referred_table'] != 'users' or keys[0]['referred_columns'] != ['id']
            or keys[0]['options'].get('ondelete') != 'CASCADE'):
        raise MigrationError('MFA code attempt owner differs')
