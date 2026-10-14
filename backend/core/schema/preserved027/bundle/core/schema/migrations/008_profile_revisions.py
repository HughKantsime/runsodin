"""Add empty profile content history without inventing past revisions."""
import re
from sqlalchemy import MetaData, Table, Column, Integer, Text, DateTime, ForeignKey, CheckConstraint, UniqueConstraint, inspect, text
from core.schema.migrator import validate_column_shape

MIGRATION_ID = 'python:008-profile-revisions'
_METADATA = MetaData()
Table('printer_profiles', _METADATA, Column('id', Integer, primary_key=True))
_TABLE = Table('printer_profile_revisions', _METADATA,
    Column('id', Integer, primary_key=True),
    Column('profile_id', Integer, ForeignKey('printer_profiles.id', ondelete='CASCADE'), nullable=False),
    Column('revision_number', Integer, nullable=False),
    Column('raw_content', Text, nullable=False),
    Column('file_format', Text, nullable=False),
    Column('slicer', Text, nullable=False),
    Column('category', Text, nullable=False),
    Column('changelog', Text, nullable=True),
    Column('created_by', Integer, nullable=True),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=text('CURRENT_TIMESTAMP')),
    UniqueConstraint('profile_id', 'revision_number', name='profile_revision_number_unique'),
    CheckConstraint('revision_number > 0', name='profile_revision_number_positive'))


def _normal(value):
    return re.sub(r'[\s()"]', '', str(value)).lower()


def validate(connection):
    inspector = inspect(connection)
    actual = {column['name']: column for column in inspector.get_columns(_TABLE.name)}
    if set(actual) != set(_TABLE.columns.keys()):
        raise RuntimeError('Profile revision column shape mismatch')
    for column in _TABLE.columns:
        definition = str(column.type.compile(dialect=connection.dialect))
        if not column.nullable:
            definition += ' NOT NULL'
        if column.server_default is not None:
            definition += ' DEFAULT ' + str(column.server_default.arg)
        if column.name == 'id' and connection.dialect.name == 'postgresql':
            owned_sequence = connection.execute(text("SELECT pg_get_serial_sequence('printer_profile_revisions','id')")).scalar_one()
            if not owned_sequence:
                raise RuntimeError('Profile revision ID sequence shape mismatch')
            expected = connection.execute(text("SELECT format('nextval(%L::regclass)', CAST(CAST(:sequence AS regclass) AS text))"), {'sequence': owned_sequence}).scalar_one()
            if actual['id'].get('default') != expected:
                raise RuntimeError('Profile revision ID default shape mismatch')
            definition += ' DEFAULT ' + expected
        validate_column_shape(connection, _TABLE.name, column.name, definition)
    if inspector.get_pk_constraint(_TABLE.name).get('constrained_columns') != ['id']:
        raise RuntimeError('Profile revision primary key shape mismatch')
    fks = inspector.get_foreign_keys(_TABLE.name)
    if any(f.get('referred_schema') not in {None, inspector.default_schema_name} for f in fks):
        raise RuntimeError('Profile revision foreign key schema shape mismatch')
    if len(fks) != 1 or (fks[0]['constrained_columns'], fks[0]['referred_table'], fks[0]['referred_columns'], fks[0].get('options', {}).get('ondelete')) != (['profile_id'], 'printer_profiles', ['id'], 'CASCADE'):
        raise RuntimeError('Profile revision foreign key shape mismatch')
    if connection.dialect.name == 'postgresql':
        canonical_target = connection.execute(text("""SELECT c.confrelid = p.oid
            FROM pg_constraint c JOIN pg_class h ON h.oid=c.conrelid
            JOIN pg_class p ON p.relnamespace=h.relnamespace AND p.relname='printer_profiles'
            WHERE h.oid=to_regclass('printer_profile_revisions') AND c.contype='f'""")).scalar_one()
        if not canonical_target:
            raise RuntimeError('Profile revision foreign key target shape mismatch')
    unique = inspector.get_unique_constraints(_TABLE.name)
    if len(unique) != 1 or unique[0]['column_names'] != ['profile_id', 'revision_number']:
        raise RuntimeError('Profile revision unique shape mismatch')
    checks = inspector.get_check_constraints(_TABLE.name)
    if len(checks) != 1 or _normal(checks[0]['sqltext']) != 'revision_number>0':
        raise RuntimeError('Profile revision check shape mismatch')


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    if _TABLE.name in inspect(connection).get_table_names():
        validate(connection)
    else:
        _TABLE.create(connection)
    validate(connection)
