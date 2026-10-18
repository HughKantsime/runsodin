"""Persist measurements already produced by MQTT and Moonraker terminal handlers."""
from sqlalchemy import inspect, text
from core.schema.migrator import validate_column_shape

MIGRATION_ID = 'python:006-print-job-measurements'
COLUMNS = {'print_duration_seconds': 'REAL', 'filament_used_g': 'REAL'}


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    existing = {column['name'] for column in inspect(connection).get_columns('print_jobs')}
    # Reject incompatible adopted columns before any additive mutation.
    for name, definition in COLUMNS.items():
        if name in existing:
            validate_column_shape(connection, 'print_jobs', name, definition)
    for name, definition in COLUMNS.items():
        if name not in existing:
            connection.execute(text(f'ALTER TABLE print_jobs ADD COLUMN {name} {definition}'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input


def validate(connection):
    for name, definition in COLUMNS.items():
        validate_column_shape(connection, 'print_jobs', name, definition)
