"""Migration 032: quiet-hours deferred deliveries table (additive; no data rewrite).

Records external (email/push/webhook) deliveries suppressed by quiet hours, so the
digest can deliver them after the window. The first registered post-027 migration
(bootstrap.POST027_MIGRATIONS). The table lives on its own MetaData
(core/quiet_hours_deferral_schema.py), so it is listed in RAW_REQUIRED_TABLES/COLUMNS
for schema validation and the backup inventory, and the ORM baselines are unchanged.
It cannot fail because of existing user data: it only creates the table if absent
and validates its exact shape. User/org deletion remove its rows
(modules/organizations/routes_users.delete_deferred_deliveries) and retention purges
them (modules/notifications/deferred_deliveries.purge).
"""
from core import quiet_hours_deferral_schema as schema

MIGRATION_ID = 'python:032-quiet-hours-deferred-deliveries'


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    schema.create(connection)
    schema.validate(connection)


def validate(connection):
    schema.validate(connection)
