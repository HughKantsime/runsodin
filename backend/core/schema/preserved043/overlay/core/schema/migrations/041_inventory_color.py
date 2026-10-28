"""Add spool-owned human color names without rewriting legacy inventory."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:041-inventory-color'


def apply(connection):
    if 'color_name' not in {c['name'] for c in inspect(connection).get_columns('spools')}:
        connection.exec_driver_sql('ALTER TABLE spools ADD COLUMN color_name VARCHAR(100)')
    validate(connection)


def validate(connection):
    column = next((c for c in inspect(connection).get_columns('spools') if c['name'] == 'color_name'), None)
    if column is None or str(column['type']).upper() != 'VARCHAR(100)' or not column['nullable'] or column.get('default') is not None:
        raise MigrationError('Spool color name shape differs')
