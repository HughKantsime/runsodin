"""Add empty unlink receipts without inferring deletion authority or progress."""
import hashlib
import importlib
from pathlib import Path
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError
from core.timelapse_unlink_schema import TABLES, unlinks

MIGRATION_ID = 'python:018-timelapse-unlinks'
SCHEMA_SOURCE_SHA256 = '72ca5b022067d86e5892230362046dabfd319319a81a3ebfddc919c70ffa3a25'
HELPER_SOURCE_SHA256 = '6e501c733473b89064a11d12363d853f23381156de90182cff4a0aefa9cc1346'
_contract = importlib.import_module('core.schema.migrations.017_timelapse_recovery')


def _source():
    source = Path(__file__).resolve().parents[2] / 'timelapse_unlink_schema.py'
    helper = Path(__file__).with_name('017_timelapse_recovery.py')
    if hashlib.sha256(source.read_bytes()).hexdigest() != SCHEMA_SOURCE_SHA256 or hashlib.sha256(helper.read_bytes()).hexdigest() != HELPER_SOURCE_SHA256:
        raise MigrationError('Unlink source contract changed')


def _retained(connection):
    if not inspect(connection).has_table(unlinks.name):
        return
    invalid = connection.execute(text("""SELECT 1 FROM timelapse_retirement_unlinks u
        LEFT JOIN timelapse_retirement_objects o ON o.operation_id=u.operation_id AND o.object_index=u.object_index
        LEFT JOIN timelapse_retirement_moves m ON m.operation_id=u.operation_id AND m.object_index=u.object_index
        WHERE o.operation_id IS NULL OR m.operation_id IS NULL OR m.kind<>o.kind
        OR m.phase<>'staged' OR m.verified_ctime_ns IS NULL
        OR NOT ((u.phase='unlink_pending' AND o.state='staged')
            OR (u.phase='unlink_confirmed' AND o.state='unlink_confirmed')
            OR (u.phase='fsync_confirmed' AND o.state='fsync_confirmed')
            OR (u.phase='retired' AND o.state='retired')) LIMIT 1""")).first()
    if invalid:
        raise MigrationError('Unlink retained phase authority differs')


def validate(connection):
    _source()
    _contract.validate(connection)
    for table in TABLES:
        if not inspect(connection).has_table(table.name):
            raise MigrationError('Unlink table missing')
        _contract._table(connection, table)
    _retained(connection)


def apply(connection):
    _source()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    _contract.validate(connection)
    for table in TABLES:
        if inspect(connection).has_table(table.name):
            _contract._table(connection, table)
    _retained(connection)
    for table in TABLES:
        table.create(connection, checkfirst=True)
    validate(connection)
