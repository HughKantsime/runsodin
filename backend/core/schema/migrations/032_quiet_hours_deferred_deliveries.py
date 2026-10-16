"""Migration 032: quiet-hours deferred deliveries table (additive; no data rewrite).

Records external (email/push/webhook) deliveries suppressed by quiet hours, so the
digest can deliver them after the window. The first registered post-027 migration
(bootstrap.POST027_MIGRATIONS). The table lives on its own MetaData
(core/quiet_hours_deferral_schema.py), so it is listed in RAW_REQUIRED_TABLES/COLUMNS
for schema validation and the backup inventory, and the ORM baselines are unchanged.
It cannot fail because of existing user data: it only creates the table if absent
and validates its exact shape. The schema module's bytes are pinned below. User/org deletion remove its rows
(modules/organizations/routes_users.delete_deferred_deliveries) and retention purges
them (modules/notifications/deferred_deliveries.purge).
"""
import hashlib
from pathlib import Path

from core import quiet_hours_deferral_schema as schema
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:032-quiet-hours-deferred-deliveries'
# The DDL lives in the schema module this migration delegates to; its bytes are pinned
# like the migration's own (the 027 pattern), so a schema change needs a new migration.
SOURCE_SHA256 = 'f98ad4f69c97c1b5caf1ef8a8b66e9390ebbb1291f957419fcd0fe5b3dc56314'


def _source():
    source = Path(__file__).resolve().parents[2] / 'quiet_hours_deferral_schema.py'
    if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise MigrationError('Deferred delivery schema source differs')


def apply(connection):
    _source()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    schema.create(connection)
    schema.validate(connection)


def validate(connection):
    _source()
    schema.validate(connection)
