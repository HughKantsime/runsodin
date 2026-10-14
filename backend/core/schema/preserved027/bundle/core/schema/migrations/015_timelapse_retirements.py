"""Add empty durable media-retirement evidence; preserve all legacy media rows."""
import hashlib
import re
import uuid
from pathlib import Path
from sqlalchemy import CheckConstraint, Column, MetaData, Table, UniqueConstraint, inspect, text
from core.timelapse_retirement_schema import intent, objects
from core.schema.migrator import MigrationError, validate_column_shape

MIGRATION_ID = 'python:015-timelapse-retirements'
SCHEMA_SOURCE_SHA256 = '8378fa6a133a595e2c2817a8b37f7e5a748e5c58a230a7c6ba45ea06f9f7ae7d'
TABLES = (intent, objects)


def _validate_source():
    source = Path(__file__).resolve().parents[2] / 'timelapse_retirement_schema.py'
    if hashlib.sha256(source.read_bytes()).hexdigest() != SCHEMA_SOURCE_SHA256:
        raise MigrationError('Retirement source contract changed')


def _normalize(value):
    parts = re.split(r"('(?:''|[^'])*')", str(value))
    return ''.join(part if index % 2 else re.sub(r'\s+', '', part).lower() for index, part in enumerate(parts))


def _expected_checks(connection, table):
    name = '_odin_retirement_contract_' + uuid.uuid4().hex[:10]
    template = Table(name, MetaData(),
        *(Column(column.name, column.type, nullable=column.nullable) for column in table.columns),
        *(CheckConstraint(str(check.sqltext), name=name+'_'+check.name) for check in table.constraints if isinstance(check, CheckConstraint)),
        prefixes=['TEMPORARY'])
    template.create(connection)
    try:
        schema = 'temp' if connection.dialect.name == 'sqlite' else connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one()
        return {check['name'].removeprefix(name+'_'): _normalize(check['sqltext']) for check in inspect(connection).get_check_constraints(name, schema=schema)}
    finally:
        template.drop(connection)


def _validate_table(connection, table):
    inspector = inspect(connection)
    actual = inspector.get_columns(table.name)
    if {column['name'] for column in actual} != set(table.columns.keys()):
        raise MigrationError('Retirement columns differ')
    for column in actual:
        expected = table.columns[column['name']]
        if str(column['type'].compile(dialect=connection.dialect)).upper() != str(expected.type.compile(dialect=connection.dialect)).upper() or column['nullable'] != expected.nullable:
            raise MigrationError('Retirement column shape differs')
        default = column.get('default')
        if expected.server_default is None and default is not None:
            raise MigrationError('Retirement unexpected default')
        if expected.server_default is not None and str(default).lower() not in ('current_timestamp', 'now()'):
            raise MigrationError('Retirement timestamp default differs')
    if inspector.get_pk_constraint(table.name)['constrained_columns'] != list(table.primary_key.columns.keys()):
        raise MigrationError('Retirement primary key differs')
    expected_unique = {tuple(item.columns.keys()) for item in table.constraints if isinstance(item, UniqueConstraint)}
    actual_unique = {tuple(item['column_names']) for item in inspector.get_unique_constraints(table.name)}
    for item in inspector.get_indexes(table.name):
        if item.get('unique'):
            options = item.get('dialect_options', {})
            if item.get('expressions') or any(value is not None for key,value in options.items() if key.endswith('_where')) or any(name is None for name in item['column_names']):
                raise MigrationError('Retirement conditional uniqueness is not allowed')
            actual_unique.add(tuple(item['column_names']))
    if actual_unique != expected_unique:
        raise MigrationError('Retirement uniqueness differs')
    actual_fks = inspector.get_foreign_keys(table.name)
    if table is intent:
        if actual_fks:raise MigrationError('Retirement intent must outlive metadata')
    elif len(actual_fks) != 1 or actual_fks[0]['constrained_columns'] != ['operation_id'] or actual_fks[0]['referred_table'] != intent.name or actual_fks[0]['referred_columns'] != ['operation_id'] or actual_fks[0].get('options', {}).get('ondelete') != 'RESTRICT' or actual_fks[0].get('options', {}).get('deferrable'):
        raise MigrationError('Retirement object parent differs')
    expected_checks = _expected_checks(connection, table)
    actual_checks = {item['name']:_normalize(item['sqltext']) for item in inspector.get_check_constraints(table.name)}
    if actual_checks != expected_checks:
        raise MigrationError('Retirement evidence checks differ')
    if connection.dialect.name == 'postgresql':
        checks = connection.execute(text("SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"), {'table':table.name}).scalars().all()
        if len(checks) != len(expected_checks) or any(value is not True for value in checks):
            raise MigrationError('Retirement checks are not validated')
        if table is objects:
            foreign = connection.execute(text("""SELECT f.convalidated, f.condeferrable, f.confrelid=parent.oid
                FROM pg_constraint f JOIN pg_class child ON child.oid=f.conrelid
                JOIN pg_class parent ON parent.relname=:parent AND parent.relnamespace=child.relnamespace AND parent.relkind='r'
                WHERE f.conrelid=to_regclass(:table) AND f.contype='f'"""), {'parent':intent.name,'table':table.name}).all()
            if foreign != [(True,False,True)]:raise MigrationError('Retirement object foreign authority differs')
    else:
        if connection.exec_driver_sql("PRAGMA integrity_check('"+table.name+"')").scalars().all() != ['ok']:
            raise MigrationError('Retirement retained evidence violates contract')
        if table is objects and connection.exec_driver_sql("PRAGMA foreign_key_check('timelapse_retirement_objects')").fetchall():
            raise MigrationError('Retirement retained objects lack parent authority')


def _validate_nonce(connection, required=False):
    columns = {column['name'] for column in inspect(connection).get_columns('timelapses')}
    if 'retirement_operation_id' in columns:
        validate_column_shape(connection, 'timelapses', 'retirement_operation_id', 'VARCHAR(64)')
        populated = connection.execute(text('SELECT 1 FROM timelapses WHERE retirement_operation_id IS NOT NULL LIMIT 1')).first()
        if populated:
            if 'printer_id' not in columns:
                raise MigrationError('Timelapse nonce lacks printer identity')
            if not inspect(connection).has_table(intent.name):
                raise MigrationError('Timelapse nonce lacks retirement authority')
            inconsistent = connection.execute(text("""SELECT 1 FROM timelapses t
                LEFT JOIN timelapse_retirements r ON r.operation_id=t.retirement_operation_id
                WHERE t.retirement_operation_id IS NOT NULL AND
                (r.operation_id IS NULL OR r.timelapse_id<>t.id OR r.filename<>t.filename
                 OR t.printer_id IS NULL OR r.printer_id<>t.printer_id
                 OR r.state='complete' OR t.status IS NULL OR t.status<>'deleting') LIMIT 1""")).first()
            if inconsistent:raise MigrationError('Timelapse retirement nonce authority differs')

    elif required:
        raise MigrationError('Timelapse retirement nonce is missing')


def validate(connection):
    _validate_source()
    _validate_nonce(connection, required=True)
    for table in TABLES:
        if not inspect(connection).has_table(table.name):raise MigrationError('Retirement table is missing')
        _validate_table(connection, table)


def apply(connection):
    _validate_source()
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    _validate_nonce(connection)
    # Validate all adopted contracts before adding any retained schema objects.
    for table in TABLES:
        if inspect(connection).has_table(table.name):_validate_table(connection, table)
    intent.create(connection, checkfirst=True)
    objects.create(connection, checkfirst=True)
    if 'retirement_operation_id' not in {column['name'] for column in inspect(connection).get_columns('timelapses')}:
        connection.exec_driver_sql('ALTER TABLE timelapses ADD COLUMN retirement_operation_id VARCHAR(64)')
    validate(connection)
