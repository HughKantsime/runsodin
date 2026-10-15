"""Unregistered030 one ODIN spool per Spoolman spool: additive partial unique index.

Spoolman is a single external instance per install, so the link is unique install-wide
(not per org). Existing duplicate links are refused loudly instead of being rewritten.
Registration (migrator allowlist + bootstrap call) changes the frozen restore-contract
sources (pre026_restore_contract SOURCE_PINS), so it ships as a separate qualified batch;
until then the API enforces uniqueness (inventory/routes/spools.py).
"""
import re
from sqlalchemy import text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:030-spoolman-link-uniqueness'
TABLE = 'spools'
COLUMN = 'spoolman_spool_id'
INDEX = 'ux_spools_spoolman_spool_id'
DDL = f'CREATE UNIQUE INDEX {INDEX} ON {TABLE} ({COLUMN}) WHERE {COLUMN} IS NOT NULL'


def _definition(connection):
    if connection.dialect.name == 'sqlite':
        return connection.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n AND tbl_name=:t"),
                                  {'n': INDEX, 't': TABLE}).scalar()
    if connection.dialect.name == 'postgresql':
        return connection.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=:n AND tablename=:t"),
                                  {'n': INDEX, 't': TABLE}).scalar()
    raise MigrationError(f'Unsupported dialect for {MIGRATION_ID}')


def _normalized(sql):
    # PostgreSQL's indexdef adds schema, access method and parentheses around the predicate.
    value = str(sql or '').lower().replace('public.', '').replace(' using btree', '')
    value = re.sub(r'["()]', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def validate(connection):
    actual = _definition(connection)
    if actual is None:
        raise MigrationError(f'Missing index {INDEX}')
    if _normalized(actual) != _normalized(DDL):
        raise MigrationError(f'Migration index shape mismatch: {INDEX}')


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    if _definition(connection) is None:
        duplicates = connection.execute(text(
            f'SELECT {COLUMN} FROM {TABLE} WHERE {COLUMN} IS NOT NULL GROUP BY {COLUMN} HAVING COUNT(*) > 1 ORDER BY {COLUMN}'
        )).scalars().all()
        if duplicates:
            raise MigrationError(
                'Several spools link the same Spoolman spool; unlink all but one before upgrading: '
                + ', '.join(str(value) for value in duplicates[:20]))
        connection.execute(text(DDL))
    validate(connection)
