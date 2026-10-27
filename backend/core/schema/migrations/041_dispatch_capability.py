"""Installed nozzle observations and narrowly scoped correction of the H2D default."""
from sqlalchemy import inspect, text
from core.schema.migrator import validate_column_shape

MIGRATION_ID = 'python:041-dispatch-capability'


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    if 'installed_nozzles' not in {c['name'] for c in inspect(connection).get_columns('printers')}:
        connection.execute(text('ALTER TABLE printers ADD COLUMN installed_nozzles JSON'))
    # The only shipped wrong default was H2D 320x320, including Pro because
    # the old UI matched the substring "h2d". Preserve every other stored pair.
    connection.execute(text("UPDATE printers SET bed_x_mm=350 WHERE api_type='bambu' "
        "AND bed_x_mm=320 AND bed_y_mm=320 "
        "AND (lower(trim(COALESCE(NULLIF(trim(machine_type),''),model,''))) IN "
        "('h2d','h2d pro','o1d','o1e','bambu lab h2d','bambu lab h2d pro'))"))
    validate(connection)


def validate(connection):
    validate_column_shape(connection, 'printers', 'installed_nozzles', 'JSON')
