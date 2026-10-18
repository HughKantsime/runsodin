"""Record database receipt time for new monitor observations, never backfill."""
from sqlalchemy import inspect,text
from core.schema.migrator import validate_column_shape,translate_statement

MIGRATION_ID='python:013-observation-received-time'
TABLE='print_jobs'
COLUMN='observed_received_at'
DEFINITION='DATETIME'


def validate(connection):
    validate_column_shape(connection,TABLE,COLUMN,translate_statement(DEFINITION,connection.dialect.name))


def apply(connection):
    if connection.dialect.name=='sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    columns={column['name'] for column in inspect(connection).get_columns(TABLE)}
    if COLUMN not in columns:
        connection.execute(text(translate_statement(f'ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DEFINITION}',connection.dialect.name)))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
    validate(connection)
