"""Add pause/cancellation labels to PostgreSQL's shared alert enum.

SQLite already stores these values as VARCHAR. No stored row is rewritten and
preferences are backfilled by the application, after this transaction commits.
"""
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import ENUM
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:045-pause-cancel-alerts'
LEGACY = ('print_complete', 'print_failed', 'printer_error', 'spool_low',
          'maintenance_overdue', 'job_submitted', 'job_approved', 'job_rejected',
          'spaghetti_detected', 'first_layer_issue', 'detachment_detected',
          'bed_cooled', 'queue_added', 'queue_skipped', 'queue_failed_start')
CURRENT = LEGACY[:2] + ('print_paused', 'print_cancelled') + LEGACY[2:]


def _shape(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported alert enum dialect')
    inspector = inspect(connection)
    for table in ('alerts', 'alert_preferences'):
        column = next((c for c in inspector.get_columns(table) if c['name'] == 'alert_type'), None)
        if column is None:
            raise MigrationError('Alert type column missing')
        datatype = column['type']
        if connection.dialect.name == 'postgresql':
            if not isinstance(datatype, ENUM) or datatype.name != 'alerttype':
                raise MigrationError('Alert enum column shape differs')
        elif str(datatype).upper() != 'VARCHAR(19)':
            raise MigrationError('Alert type column shape differs')
    if connection.dialect.name == 'postgresql':
        enums = [e for e in inspector.get_enums(schema='public') if e['name'] == 'alerttype']
        if len(enums) != 1:
            raise MigrationError('Alert enum missing')
        return tuple(enums[0]['labels'])
    return CURRENT


def apply(connection):
    labels = _shape(connection)
    if labels not in (LEGACY, CURRENT):
        raise MigrationError('Alert enum labels differ')
    if connection.dialect.name == 'postgresql' and labels == LEGACY:
        connection.exec_driver_sql("ALTER TYPE public.alerttype ADD VALUE 'print_paused' AFTER 'print_failed'")
        connection.exec_driver_sql("ALTER TYPE public.alerttype ADD VALUE 'print_cancelled' AFTER 'print_paused'")
    validate(connection)


def validate(connection):
    if _shape(connection) != CURRENT:
        raise MigrationError('Alert enum labels differ')
