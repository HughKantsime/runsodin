"""Add immutable recipe/inspection evidence and prepared stock additions.

No historical rows are rewritten. Receipts are deliberately retained independently
of mutable job, product and organization rows, so deletes cannot erase deduplication.
Only integer owner keys, opaque references and product/process snapshots are stored;
no credentials, customer or actor data. Tenant deletion is refused while evidence
exists, rather than silently orphaning or dropping accepted production history.
"""
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, UniqueConstraint, inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:035-production-acceptance'
metadata = MetaData()
recipes = Table('production_recipes', metadata,
    Column('id', String(32), primary_key=True, nullable=False),
    Column('owner', Integer, nullable=False), Column('definition', Text, nullable=False))
receipts = Table('production_acceptances', metadata,
    Column('id', String(32), primary_key=True, nullable=False),
    Column('owner', Integer, nullable=False),
    Column('job_uid', String(64), nullable=False, unique=True),
    Column('claim_token', String(32), nullable=False, unique=True),
    Column('recipe_id', String(32), nullable=False),
    Column('request_hash', String(64), nullable=False),
    Column('definition', Text, nullable=False))
deliveries = Table('production_stock_additions', metadata,
    Column('id', String(32), primary_key=True, nullable=False),
    Column('owner', Integer, nullable=False),
    Column('receipt_id', String(32), nullable=False, unique=True),
    Column('destination', Text, nullable=False), Column('request_body', Text, nullable=False),
    Column('state', String(20), nullable=False), Column('attempts', Integer, nullable=False))
TABLES = (recipes, receipts, deliveries)
EXPECTED_COLUMNS = {t.name: frozenset(c.name for c in t.columns) for t in TABLES}


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    metadata.create_all(connection, tables=TABLES, checkfirst=True)
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    for table in TABLES:
        if not inspector.has_table(table.name):
            raise MigrationError('Missing production acceptance table')
        cols = {c['name']: c for c in inspector.get_columns(table.name)}
        if set(cols) != EXPECTED_COLUMNS[table.name]:
            raise MigrationError('Production acceptance column shape differs')
        for expected in table.columns:
            actual = cols[expected.name]
            if actual['nullable'] or actual.get('default') is not None:
                raise MigrationError('Production acceptance nullability/default differs')
            actual_type = actual['type']
            if isinstance(expected.type, Text):
                valid = isinstance(actual_type, Text)
            elif isinstance(expected.type, String):
                valid = isinstance(actual_type, String) and actual_type.length == expected.type.length
            else:
                valid = isinstance(actual_type, Integer)
            if not valid:
                raise MigrationError('Production acceptance column type differs')
        if inspector.get_pk_constraint(table.name).get('constrained_columns') != ['id']:
            raise MigrationError('Production acceptance primary key differs')
        unique = {tuple(c.get('column_names', ())) for c in inspector.get_unique_constraints(table.name)}
        expected_unique = {tuple(c.name for c in k.columns) for k in table.constraints if isinstance(k, UniqueConstraint)}
        if unique != expected_unique or inspector.get_foreign_keys(table.name) or inspector.get_check_constraints(table.name):
            raise MigrationError('Production acceptance constraints differ')
