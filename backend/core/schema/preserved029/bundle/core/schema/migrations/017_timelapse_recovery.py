"""Add empty identity/digest/move records; never infer old retirement evidence."""
import hashlib
import importlib
from pathlib import Path
from sqlalchemy import inspect,text
from core.schema.migrator import MigrationError
from core.timelapse_recovery_schema import TABLES,moves

MIGRATION_ID='python:017-timelapse-recovery'
SCHEMA_SOURCE_SHA256='fa1f802e9581df100268682f195753c2c929a89e6dd911a18ef0312f396a3ae3'
HELPER_SOURCE_SHA256='a31f9b87ac68b1009178fdeba9c9043cccd780f00c08be3113c40822f261f68e'
_contract=importlib.import_module('core.schema.migrations.015_timelapse_retirements')


def _source():
    source=Path(__file__).resolve().parents[2]/'timelapse_recovery_schema.py'
    helper=Path(__file__).with_name('015_timelapse_retirements.py')
    if hashlib.sha256(source.read_bytes()).hexdigest()!=SCHEMA_SOURCE_SHA256 or hashlib.sha256(helper.read_bytes()).hexdigest()!=HELPER_SOURCE_SHA256:
        raise MigrationError('Recovery source contract changed')


def _table(connection,table):
    inspector=inspect(connection);actual=inspector.get_columns(table.name)
    if {value['name'] for value in actual}!=set(table.columns.keys()):raise MigrationError('Recovery columns differ')
    for value in actual:
        expected=table.columns[value['name']]
        if str(value['type'].compile(dialect=connection.dialect)).upper()!=str(expected.type.compile(dialect=connection.dialect)).upper() or value['nullable']!=expected.nullable or value.get('default') is not None:
            raise MigrationError('Recovery column shape differs')
    if inspector.get_pk_constraint(table.name)['constrained_columns']!=list(table.primary_key.columns.keys()):raise MigrationError('Recovery primary key differs')
    if inspector.get_indexes(table.name) or inspector.get_unique_constraints(table.name):raise MigrationError('Recovery indexes differ')
    expected_fk=next(iter(table.foreign_key_constraints))
    foreign=inspector.get_foreign_keys(table.name)
    if len(foreign)!=1:raise MigrationError('Recovery foreign authority differs')
    actual_fk=foreign[0];elements=list(expected_fk.elements)
    schema=inspector.default_schema_name
    if actual_fk['constrained_columns']!=list(expected_fk.column_keys) or actual_fk['referred_table']!=elements[0].column.table.name or actual_fk['referred_columns']!=[value.column.name for value in elements] or actual_fk.get('referred_schema') not in (None,schema) or actual_fk.get('options',{}).get('ondelete')!='RESTRICT' or actual_fk.get('options',{}).get('onupdate') not in (None,'NO ACTION') or actual_fk.get('options',{}).get('deferrable'):
        raise MigrationError('Recovery foreign authority differs')
    expected=_contract._expected_checks(connection,table)
    checks={value['name']:_contract._normalize(value['sqltext']) for value in inspector.get_check_constraints(table.name)}
    if checks!=expected:raise MigrationError('Recovery checks differ')
    if connection.dialect.name=='postgresql':
        validated=connection.execute(text("SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"),{'table':table.name}).scalars().all()
        if len(validated)!=len(expected) or any(value is not True for value in validated):raise MigrationError('Recovery checks unvalidated')
        target=elements[0].column.table.name
        authority=connection.execute(text("""SELECT f.convalidated,f.condeferrable,f.confrelid=parent.oid,f.confupdtype='a'
            FROM pg_constraint f JOIN pg_class child ON child.oid=f.conrelid
            JOIN pg_class parent ON parent.relname=:parent AND parent.relnamespace=child.relnamespace AND parent.relkind='r'
            WHERE f.conrelid=to_regclass(:table) AND f.contype='f'"""),{'table':table.name,'parent':target}).all()
        if authority!=[(True,False,True,True)]:raise MigrationError('Recovery foreign checks differ')
    else:
        if connection.exec_driver_sql("PRAGMA integrity_check('"+table.name+"')").scalars().all()!=['ok'] or connection.exec_driver_sql("PRAGMA foreign_key_check('"+table.name+"')").first() is not None:
            raise MigrationError('Recovery retained evidence invalid')


def _retained(connection):
    if inspect(connection).has_table(moves.name):
        invalid=connection.execute(text("""SELECT 1 FROM timelapse_retirement_moves m
            LEFT JOIN timelapse_retirement_objects o ON o.operation_id=m.operation_id AND o.object_index=m.object_index
            WHERE o.operation_id IS NULL OR o.kind<>m.kind OR o.state='originally_absent' LIMIT 1""")).first()
        if invalid:raise MigrationError('Recovery move source differs')


def validate(connection):
    _source();_contract.validate(connection)
    for table in TABLES:
        if not inspect(connection).has_table(table.name):raise MigrationError('Recovery table missing')
        _table(connection,table)
    _retained(connection)


def apply(connection):
    _source()
    if connection.dialect.name=='sqlite' and not connection.connection.driver_connection.in_transaction:connection.exec_driver_sql('BEGIN IMMEDIATE')
    _contract.validate(connection)
    for table in TABLES:
        if inspect(connection).has_table(table.name):_table(connection,table)
    _retained(connection)
    for table in TABLES:table.create(connection,checkfirst=True)
    validate(connection)
