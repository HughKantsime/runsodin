"""Unregistered032 deferred quiet-hours deliveries: additive table, no data rewrite.

Records external (email/push) deliveries suppressed by quiet hours for recipients
without an in-app alert row, so the digest can deliver them after the window.
Registration (migrator allowlist + bootstrap call + RAW_REQUIRED_* / backup
inventory) changes restore-pinned sources and is designed separately in
.claude/specs/post027-migrations.md; until then the table is never created at
runtime and the digest keeps its pre-032 behaviour.

Registration checklist (for the post-027 migration mechanism):
1. Add "core.schema.migrations.032_quiet_hours_deferred_deliveries" to the
   migrator's allowed Python migrations and call it from bootstrap after 027,
   on SQLite and PostgreSQL (additive CREATE TABLE only; no data rewrite).
2. Add quiet_hours_deferred_deliveries and its columns
   (id user_id org_id scope_key channel alert_type severity title message
   printer_id job_id spool_id state outcome_reason created_at delivered_at)
   to RAW_REQUIRED_TABLES / RAW_REQUIRED_COLUMNS, so backups include it and
   restores require it; add a backup -> restore round-trip test.
3. The table is deliberately not on core.base.Base (core/quiet_hours_deferral_schema.py
   owns it on its own MetaData), so the ORM baselines do not change.
4. Remove the strict xfails in tests/test_contracts/test_cert_E_notif_quiet.py
   (test_email_only_user_receives_digest_of_suppressed_alert[orm|daemon]): with
   the table bootstrapped they pass, as test_cert_E_notif_deferred.py shows.
5. Keep test_032_is_not_registered_until_post027_mechanism in sync (invert it).
6. Already in place: user deletion and org/group deletion remove rows
   (modules/organizations/routes_users.delete_deferred_deliveries, no-op while
   the table is absent); retention purge in the digest (sent after 7 days,
   failed/pending after 30); native PostgreSQL probe in
   tests/test_contracts/test_cert_E_notif_postgres.py.
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
