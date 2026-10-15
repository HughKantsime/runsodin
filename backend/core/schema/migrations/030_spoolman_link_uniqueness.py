"""Migration 030: one ODIN spool per Spoolman spool, as an additive partial unique index.

Spoolman is a single external instance per install, so the link is unique install-wide
(not per org). Released 1.9.x schemas had no constraint, so duplicates can exist. They are
never rewritten: the migration is DEFERRABLE, so boot continues without the index, an admin
warning names the conflicting spools, the API keeps refusing new duplicates, and every
later boot retries. Registered as the first post-027 migration (bootstrap.POST027_MIGRATIONS).
"""
import re
from sqlalchemy import text
from core.schema.migrator import MigrationDeferred, MigrationError

MIGRATION_ID = 'python:030-spoolman-link-uniqueness'
DEFERRABLE = True
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
        rows = connection.execute(text(
            f'SELECT id, {COLUMN} FROM {TABLE} WHERE {COLUMN} IN '
            f'(SELECT {COLUMN} FROM {TABLE} WHERE {COLUMN} IS NOT NULL GROUP BY {COLUMN} HAVING COUNT(*) > 1) '
            f'ORDER BY {COLUMN}, id'
        )).all()
        if rows:
            # Read-only so far: deferral is raised before any write.
            linked = sorted({int(row[1]) for row in rows})
            raise MigrationDeferred(
                'Several spools link the same Spoolman spool, so the one-spool-per-Spoolman-spool '
                'index was not created. Unlink all but one spool for each listed Spoolman spool, '
                'then restart ODIN to create it.',
                detail={'spool_ids': [int(row[0]) for row in rows][:200], 'spoolman_spool_ids': linked[:200],
                        'conflicting_spoolman_spools': len(linked)})
        connection.execute(text(DDL))
    validate(connection)
