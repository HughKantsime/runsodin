"""Unregistered031: dedicated jobs.pinned_printer_id for explicit specific-printer pins.

Not yet applied at runtime: registration in migrator/bootstrap waits for the
post-027 migration mechanism (.claude/specs/post027-migrations.md), because both
files are SHA-pinned by the pre026 restore contract. Until then the pin is stored
in the job's existing target columns (target_type='specific', target_filter=<printer
id>; modules/jobs/pinning.py), which already survive every reset. On registration:
add Job.pinned_printer_id = Column(Integer, nullable=True) (no FK, so a deleted
printer leaves the job visibly blocked), pin a new ORM baseline, backfill from the
target columns below, and switch modules/jobs/pinning.py to the column.
"""
from sqlalchemy import inspect, text
from core.schema.migrator import validate_column_shape, translate_statement

MIGRATION_ID = 'python:031-job-printer-pins'
TABLE = 'jobs'
COLUMN = 'pinned_printer_id'
DEFINITION = 'INTEGER'
# Existing explicit pins live in the target columns; carry them over exactly.
BACKFILL = ("UPDATE jobs SET pinned_printer_id=CAST(target_filter AS INTEGER) "
            "WHERE pinned_printer_id IS NULL AND COALESCE(target_type,'specific')='specific' "
            "AND target_filter IS NOT NULL AND TRIM(target_filter)<>'' "
            "AND TRIM(target_filter) NOT GLOB '*[^0-9]*'")
BACKFILL_POSTGRES = ("UPDATE jobs SET pinned_printer_id=CAST(TRIM(target_filter) AS INTEGER) "
                     "WHERE pinned_printer_id IS NULL AND COALESCE(target_type,'specific')='specific' "
                     "AND TRIM(COALESCE(target_filter,'')) ~ '^[0-9]+$'")


def validate(connection):
    validate_column_shape(connection, TABLE, COLUMN, translate_statement(DEFINITION, connection.dialect.name))


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    columns = {column['name'] for column in inspect(connection).get_columns(TABLE)}
    if COLUMN not in columns:
        connection.execute(text(translate_statement(f'ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DEFINITION}',
                                                    connection.dialect.name)))
    connection.execute(text(BACKFILL_POSTGRES if connection.dialect.name == 'postgresql' else BACKFILL))
    validate(connection)
