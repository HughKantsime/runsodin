"""Add empty capture-time ownership anchors without inferring legacy owners."""
import hashlib
import importlib
from pathlib import Path
from sqlalchemy import inspect,text
from core.timelapse_ownership_schema import anchors
from core.schema.migrator import MigrationError,validate_column_shape

MIGRATION_ID='python:016-timelapse-ownership'
SCHEMA_SOURCE_SHA256='a9eba49d4f89153dc362def7c09f67fa78c807aae86ef85ca13e5291eaf90278'
HELPER_SOURCE_SHA256='a31f9b87ac68b1009178fdeba9c9043cccd780f00c08be3113c40822f261f68e'
_contract=importlib.import_module('core.schema.migrations.015_timelapse_retirements')


def _source():
    helper=Path(__file__).with_name('015_timelapse_retirements.py')
    if hashlib.sha256(helper.read_bytes()).hexdigest()!=HELPER_SOURCE_SHA256:
        raise MigrationError('Media ownership helper contract changed')
    source=Path(__file__).resolve().parents[2]/'timelapse_ownership_schema.py'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=SCHEMA_SOURCE_SHA256:
        raise MigrationError('Media ownership source contract changed')


def _table(connection):
    inspector=inspect(connection)
    columns=inspector.get_columns(anchors.name)
    if {v['name'] for v in columns}!=set(anchors.columns.keys()):raise MigrationError('Media ownership columns differ')
    for value in columns:
        expected=anchors.columns[value['name']]
        if str(value['type'].compile(dialect=connection.dialect)).upper()!=str(expected.type.compile(dialect=connection.dialect)).upper() or value['nullable']!=expected.nullable:
            raise MigrationError('Media ownership column shape differs')
        default=value.get('default')
        if expected.server_default is None and default is not None:raise MigrationError('Media ownership unexpected default')
        if expected.server_default is not None and str(default).lower() not in ('current_timestamp','now()'):raise MigrationError('Media ownership timestamp default differs')
    if inspector.get_pk_constraint(anchors.name)['constrained_columns']!=['operation_id']:raise MigrationError('Media ownership primary key differs')
    if inspector.get_foreign_keys(anchors.name) or inspector.get_unique_constraints(anchors.name) or inspector.get_indexes(anchors.name):raise MigrationError('Media ownership must retain independent history')
    expected=_contract._expected_checks(connection,anchors)
    actual={v['name']:_contract._normalize(v['sqltext']) for v in inspector.get_check_constraints(anchors.name)}
    if actual!=expected:raise MigrationError('Media ownership evidence checks differ')
    if connection.dialect.name=='postgresql':
        validated=connection.execute(text("SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"),{'table':anchors.name}).scalars().all()
        if len(validated)!=len(expected) or any(v is not True for v in validated):raise MigrationError('Media ownership checks not validated')


def _nonce(connection):
    inspector=inspect(connection)
    columns={v['name']:v for v in inspector.get_columns('timelapses')}
    if 'ownership_operation_id' not in columns:return
    validate_column_shape(connection,'timelapses','ownership_operation_id','VARCHAR(64)')
    column=columns['ownership_operation_id']
    if column['nullable'] is not True or column.get('default') is not None:raise MigrationError('Media ownership nonce must be nullable and default free')
    if not {'printer_id','print_job_id','filename'}.issubset(columns):raise MigrationError('Media ownership live identity incomplete')
    rows=connection.execute(text('SELECT id,printer_id,print_job_id,filename,ownership_operation_id FROM timelapses WHERE ownership_operation_id IS NOT NULL')).all()
    if not rows:return
    if not inspector.has_table(anchors.name):raise MigrationError('Media ownership anchor missing')
    if not {'printer_id','print_job_id','filename'}.issubset(columns):raise MigrationError('Media ownership live identity incomplete')
    for live in rows:
        if type(live[4]) is not str or not 1<=len(live[4])<=64:raise MigrationError('Media ownership nonce invalid')
        owner=connection.execute(text('SELECT timelapse_id,printer_id,scheduled_job_id,filename FROM timelapse_ownership_anchors WHERE operation_id=:operation'),{'operation':live[4]}).first()
        if owner is None or tuple(owner)!=tuple(live[:4]):raise MigrationError('Media ownership live tuple differs')


def validate(connection):
    _source()
    if not inspect(connection).has_table(anchors.name):raise MigrationError('Media ownership table missing')
    _table(connection);_nonce(connection)
    if 'ownership_operation_id' not in {v['name'] for v in inspect(connection).get_columns('timelapses')}:raise MigrationError('Media ownership nonce missing')
    if connection.dialect.name=='sqlite':
        if connection.exec_driver_sql('PRAGMA integrity_check').scalars().all()!=['ok'] or connection.exec_driver_sql('PRAGMA foreign_key_check').first() is not None:
            raise MigrationError('Media ownership database integrity differs')


def apply(connection):
    _source()
    if connection.dialect.name=='sqlite' and not connection.connection.driver_connection.in_transaction:connection.exec_driver_sql('BEGIN IMMEDIATE')
    if inspect(connection).has_table(anchors.name):_table(connection)
    _nonce(connection)
    anchors.create(connection,checkfirst=True)
    if 'ownership_operation_id' not in {v['name'] for v in inspect(connection).get_columns('timelapses')}:
        connection.exec_driver_sql('ALTER TABLE timelapses ADD COLUMN ownership_operation_id VARCHAR(64)')
    validate(connection)
