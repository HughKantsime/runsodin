"""Add empty durable sender registrations; reject incompatible adopted contracts."""
import re
import hashlib
from pathlib import Path
import uuid
from sqlalchemy import inspect,CheckConstraint,UniqueConstraint,MetaData,text
from core.dispatch_sender_schema import DispatchSenderRegistration
from core.schema.migrator import MigrationError

MIGRATION_ID='python:014-dispatch-senders'
TABLE=DispatchSenderRegistration.__table__
SCHEMA_SOURCE_SHA256='0394b9b2d3b36bd74910cf5f5632512574c355934f0509e7826a8200ea71b2ca'


def _normalized_check(value):
    # Normalize spacing only outside string literals. Keep grouping and case.
    pieces = re.split(r"('(?:''|[^'])*')", value)
    return ''.join(piece if index % 2 else re.sub(r'\s+', '', piece).lower()
                   for index, piece in enumerate(pieces))


def _expected_checks(connection):
    # Reflect the exact dialect rendering; do not guess away PostgreSQL casts
    # or parentheses. A private temporary contract is removed before return.
    name='_odin_sender_contract_'+uuid.uuid4().hex[:10]
    template=TABLE.to_metadata(MetaData(),name=name)
    template._prefixes=['TEMPORARY']
    for constraint in template.constraints:
        if constraint.name:constraint.name=name+'_'+constraint.name
    template.create(connection)
    try:
        schema='temp' if connection.dialect.name=='sqlite' else connection.execute(text(
            'SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one()
        return {item['name'].removeprefix(name+'_'):_normalized_check(item['sqltext'])
                for item in inspect(connection).get_check_constraints(name,schema=schema)}
    finally:
        template.drop(connection)


def _validate_source():
    source=Path(__file__).resolve().parents[2]/'dispatch_sender_schema.py'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=SCHEMA_SOURCE_SHA256:
        raise MigrationError('Sender registration source contract changed')


def validate(connection):
    _validate_source()
    inspector=inspect(connection)
    if not inspector.has_table(TABLE.name):
        raise MigrationError('Sender registration table is missing')
    actual=inspector.get_columns(TABLE.name)
    if {column['name'] for column in actual} != set(TABLE.columns.keys()):
        raise MigrationError('Sender registration columns differ')
    for column in actual:
        expected=TABLE.columns[column['name']]
        if str(column['type'].compile(dialect=connection.dialect)).upper()!=str(expected.type.compile(dialect=connection.dialect)).upper() or column['nullable']!=expected.nullable:
            raise MigrationError('Sender registration column shape differs')
        default=column.get('default')
        if expected.server_default is None and default is not None:
            raise MigrationError('Sender registration unexpected default')
        if expected.server_default is not None and str(default).lower() not in ('current_timestamp','now()'):
            raise MigrationError('Sender registration timestamp default differs')
    if inspector.get_pk_constraint(TABLE.name)['constrained_columns']!=['claim_token'] or inspector.get_foreign_keys(TABLE.name):
        raise MigrationError('Sender registration primary/foreign key differs')
    expected_unique={tuple(constraint.columns.keys()) for constraint in TABLE.constraints if isinstance(constraint,UniqueConstraint)}
    actual_unique={tuple(item['column_names']) for item in inspector.get_unique_constraints(TABLE.name)}
    for item in inspector.get_indexes(TABLE.name):
        if item.get('unique'):
            options=item.get('dialect_options', {})
            if item.get('expressions') or any(value is not None for key,value in options.items() if key.endswith('_where')) or any(name is None for name in item['column_names']):
                raise MigrationError('Sender registration conditional uniqueness is not allowed')
            actual_unique.add(tuple(item['column_names']))
    if actual_unique!=expected_unique:
        raise MigrationError('Sender registration uniqueness differs')
    expected_checks=_expected_checks(connection)
    actual_checks={item['name']:_normalized_check(item['sqltext']) for item in inspector.get_check_constraints(TABLE.name)}
    if actual_checks!=expected_checks:
        raise MigrationError('Sender registration state or identity checks differ')
    if connection.dialect.name=='postgresql':
        validated=connection.execute(text(
            "SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"),
            {'table':TABLE.name}).scalars().all()
        if len(validated)!=len(expected_checks) or any(value is not True for value in validated):
            raise MigrationError('Sender registration checks are not validated')
    elif connection.dialect.name=='sqlite':
        if connection.exec_driver_sql("PRAGMA integrity_check('dispatch_sender_registrations')").scalars().all()!=['ok']:
            raise MigrationError('Sender registration retained records violate the contract')



def apply(connection):
    _validate_source()
    # pysqlite otherwise autocommits DDL despite SQLAlchemy engine.begin().
    # Participate in an existing real transaction; never commit it here.
    if (connection.dialect.name=='sqlite'
            and not connection.connection.driver_connection.in_transaction):
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    if inspect(connection).has_table(TABLE.name):
        validate(connection)
    else:
        TABLE.create(connection)
        validate(connection)
