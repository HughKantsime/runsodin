"""Add nullable right-nozzle observations without manufacturing historical values."""
from sqlalchemy import inspect, text
from core.schema.migrator import validate_column_shape, translate_statement

MIGRATION_ID = 'python:010-h2d-nozzle-observations'
COLUMNS = {
    'nozzle_1_temp': 'FLOAT',
    'nozzle_1_target_temp': 'FLOAT',
    'nozzle_1_temp_observed_at': 'DATETIME',
    'nozzle_1_target_observed_at': 'DATETIME',
}


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    existing = {column['name'] for column in inspect(connection).get_columns('printers')}
    for name, definition in COLUMNS.items():
        if name in existing:
            validate_column_shape(connection, 'printers', name, translate_statement(definition, connection.dialect.name))
    for name, definition in COLUMNS.items():
        if name not in existing:
            connection.execute(text(translate_statement(f'ALTER TABLE printers ADD COLUMN {name} {definition}', connection.dialect.name)))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
    validate(connection)


def validate(connection):
    for name, definition in COLUMNS.items():
        validate_column_shape(connection, 'printers', name, translate_statement(definition, connection.dialect.name))
