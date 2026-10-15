"""Unregistered032 deferred quiet-hours deliveries: additive table, no data rewrite.

Records external (email/push) deliveries suppressed by quiet hours for recipients
without an in-app alert row, so the digest can deliver them after the window.
Registration (migrator allowlist + bootstrap call + RAW_REQUIRED_* / backup
inventory) changes restore-pinned sources and is designed separately in
.claude/specs/post027-migrations.md; until then the table is never created at
runtime and the digest keeps its pre-032 behaviour.
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
