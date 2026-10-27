"""Installed nozzle observations and narrowly scoped correction of the H2D default."""
import json
from sqlalchemy import inspect, text
from core.schema.migrator import validate_column_shape

MIGRATION_ID = 'python:042-dispatch-capability'


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    if 'installed_nozzles' not in {c['name'] for c in inspect(connection).get_columns('printers')}:
        connection.execute(text('ALTER TABLE printers ADD COLUMN installed_nozzles JSON'))
    # The only shipped wrong default was H2D 320x320, including Pro because
    # the old UI matched the substring "h2d". Preserve every other stored pair.
    changed = connection.execute(text("UPDATE printers SET bed_x_mm=350 WHERE api_type='bambu' "
        "AND bed_x_mm=320 AND bed_y_mm=320 "
        "AND (lower(trim(COALESCE(NULLIF(trim(machine_type),''),model,''))) IN "
        "('h2d','h2d pro','o1d','o1e','bambu lab h2d','bambu lab h2d pro')) RETURNING id")).scalars().all()
    # Retain the correction in the same transaction as the write. Legacy recipe
    # receipts cannot prove equality against the newly corrected bed geometry.
    for printer_id in changed:
        connection.execute(text("""INSERT INTO audit_logs(action,entity_type,entity_id,details)
            VALUES ('printer.observed_config_changed','printer',:id,:details)"""),
            {'id': printer_id, 'details': json.dumps({
                'field': 'bed_x_mm', 'old': 320, 'new': 350, 'migration': MIGRATION_ID})})
    validate(connection)


def validate(connection):
    validate_column_shape(connection, 'printers', 'installed_nozzles', 'JSON')
