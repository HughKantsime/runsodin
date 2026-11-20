"""Store single-use MFA recovery codes for each account."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table, inspect

from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:063-mfa-recovery-codes'
metadata = MetaData()
Table('users', metadata, Column('id', Integer, primary_key=True))
codes = Table('mfa_recovery_codes', metadata,
    Column('id', Integer, primary_key=True),
    Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
    Column('code_salt', String(32), nullable=False),
    Column('code_hash', String(64), nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False),
    Column('used_at', DateTime(timezone=True)))
Index('ix_mfa_recovery_codes_user', codes.c.user_id)


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported MFA recovery code database')
    codes.create(connection, checkfirst=True)
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    columns = {c['name']: c for c in inspector.get_columns(codes.name)}
    if set(columns) != set(codes.c.keys()):
        raise MigrationError('MFA recovery code columns differ')
    for name, expected in codes.c.items():
        column = columns[name]
        if (column['type'].compile(dialect=connection.dialect) != expected.type.compile(dialect=connection.dialect)
                or column['nullable'] != expected.nullable
                or (name != 'id' and column['default'] is not None)):
            raise MigrationError('MFA recovery code column shape differs')
    if inspector.get_pk_constraint(codes.name)['constrained_columns'] != ['id']:
        raise MigrationError('MFA recovery code primary key differs')
    foreign_keys = inspector.get_foreign_keys(codes.name)
    if len(foreign_keys) != 1:
        raise MigrationError('MFA recovery code owner differs')
    fk = foreign_keys[0]
    if (fk['constrained_columns'] != ['user_id'] or fk['referred_table'] != 'users'
            or fk['referred_columns'] != ['id'] or fk['options'].get('ondelete') != 'CASCADE'):
        raise MigrationError('MFA recovery code owner differs')
    indexes = inspector.get_indexes(codes.name)
    if (len(indexes) != 1 or indexes[0]['name'] != 'ix_mfa_recovery_codes_user'
            or indexes[0]['column_names'] != ['user_id'] or indexes[0]['unique']
            or indexes[0].get('dialect_options', {}).get(connection.dialect.name + '_where') is not None
            or inspector.get_unique_constraints(codes.name)):
        raise MigrationError('MFA recovery code index differs')
