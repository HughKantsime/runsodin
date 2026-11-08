"""Explicit controlled audiences; membership is independent of tenancy."""
from sqlalchemy import (MetaData, Table, Column, Integer, String, ForeignKey, inspect)
from core.schema.migrator import MigrationError

metadata = MetaData()
# Foreign targets are resolved locally; these placeholders are never created.
Table('users', metadata, Column('id', Integer, primary_key=True))
audiences = Table('controlled_audiences', metadata,
    Column('id', Integer, primary_key=True),
    Column('name', String(100), nullable=False),
    Column('created_by', Integer, ForeignKey('users.id', ondelete='SET NULL')))
members = Table('controlled_audience_members', metadata,
    Column('audience_id', Integer, ForeignKey('controlled_audiences.id', ondelete='CASCADE'), primary_key=True),
    Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True))


def create_audiences(connection):
    metadata.create_all(connection, tables=[audiences, members])
    validate_audiences(connection)


def validate_audiences(connection):
    inspector = inspect(connection)
    for table in (audiences, members):
        columns = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(columns) != set(table.c.keys()) or any(
                str(columns[c.name]['type']) != str(c.type) or columns[c.name]['nullable'] != c.nullable
                for c in table.c):
            raise MigrationError('Controlled audience column shape differs')
        if inspector.get_pk_constraint(table.name)['constrained_columns'] != [c.name for c in table.primary_key]:
            raise MigrationError('Controlled audience primary key differs')
        actual = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']),
                   f['options'].get('ondelete')) for f in inspector.get_foreign_keys(table.name)}
        expected = {((f.parent.name,), f.column.table.name, (f.column.name,), f.ondelete) for f in table.foreign_keys}
        if actual != expected:
            raise MigrationError('Controlled audience foreign keys differ')
