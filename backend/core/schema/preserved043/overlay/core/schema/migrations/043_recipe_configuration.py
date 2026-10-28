"""Add nullable recipe identity evidence. No stored data is rewritten."""
import re
import uuid
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:043-recipe-configuration'
TABLE = 'dispatch_attempt_receipts'
ADDITIONS = {'stable_configuration_key': 'VARCHAR(64)', 'stable_key_source': 'VARCHAR(32)'}
BASE_DDL = """(
 claim_token VARCHAR(32) NOT NULL PRIMARY KEY,
 printer_id INTEGER NOT NULL,
 job_id INTEGER NOT NULL,
 configuration_key VARCHAR(64) NOT NULL,
 prepared_power_key VARCHAR(64) NOT NULL,
 observed_filename_key VARCHAR(64) NOT NULL,
 observed_print_job_id INTEGER,
 created_at {timestamp} DEFAULT CURRENT_TIMESTAMP NOT NULL,
 {extra}
 CONSTRAINT uq_dispatch_receipt_observation UNIQUE (observed_print_job_id),
 CONSTRAINT ck_dispatch_receipt_ids CHECK (printer_id>0 AND job_id>0),
 CONSTRAINT ck_dispatch_receipt_observation CHECK (observed_print_job_id IS NULL OR observed_print_job_id>0),
 CONSTRAINT ck_dispatch_receipt_keys CHECK (length(claim_token)=32 AND length(configuration_key)=64 AND length(prepared_power_key)=64 AND length(observed_filename_key)=64)
)"""


def _normalize(value):
    pieces = re.split(r"('(?:''|[^'])*')", str(value))
    return ''.join(p if i % 2 else re.sub(r'\s+', '', p).lower() for i, p in enumerate(pieces))


def _shape(connection, table, schema=None):
    inspector = inspect(connection)
    columns = [(c['name'], str(c['type'].compile(dialect=connection.dialect)).upper(), c['nullable'],
                'now' if str(c.get('default')).lower() in ('current_timestamp','now()') else c.get('default'))
               for c in inspector.get_columns(table, schema=schema)]
    uniques = sorted(tuple(c['column_names']) for c in inspector.get_unique_constraints(table, schema=schema))
    indexes = [(tuple(i['column_names']), i.get('unique'), str(i.get('dialect_options', {})))
               for i in inspector.get_indexes(table, schema=schema) if not i.get('duplicates_constraint')]
    checks = sorted((c['name'],_normalize(c['sqltext'])) for c in inspector.get_check_constraints(table, schema=schema))
    return (columns, inspector.get_pk_constraint(table, schema=schema)['constrained_columns'],
            inspector.get_foreign_keys(table, schema=schema), uniques, indexes, checks)


def _validate(connection, *, stable):
    if connection.dialect.name not in ('sqlite','postgresql'):
        raise MigrationError('Unsupported recipe configuration dialect')
    name = '_receipt_contract_' + uuid.uuid4().hex[:12]
    postgres = connection.dialect.name == 'postgresql'
    ddl = BASE_DDL.format(timestamp='TIMESTAMP WITH TIME ZONE' if postgres else 'DATETIME',
        extra=','.join(n+' '+t for n,t in ADDITIONS.items())+',' if stable else '')
    connection.exec_driver_sql(('CREATE TEMPORARY TABLE ' if postgres else 'CREATE TABLE ')+name+' '+ddl)
    try:
        schema = connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one() if postgres else None
        if _shape(connection, TABLE) != _shape(connection, name, schema):
            raise MigrationError('Dispatch receipt recipe configuration shape differs')
        if not postgres and connection.exec_driver_sql("PRAGMA integrity_check('dispatch_attempt_receipts')").scalars().all() != ['ok']:
            raise MigrationError('Dispatch receipt retained records violate the contract')
        if postgres and connection.execute(text("SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(:table) AND NOT convalidated"), {'table':TABLE}).first():
            raise MigrationError('Dispatch receipt constraints are not validated')
    finally:
        connection.exec_driver_sql('DROP TABLE '+name)


def validate_legacy(connection):
    """Validate012 on either side of043 without altering its pinned source/model."""
    columns = {c['name'] for c in inspect(connection).get_columns(TABLE)}
    _validate(connection, stable=bool(columns & ADDITIONS.keys()))


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    columns = {c['name'] for c in inspect(connection).get_columns(TABLE)}
    if columns & ADDITIONS.keys():
        validate(connection)
        return
    _validate(connection, stable=False)
    for name, datatype in ADDITIONS.items():
        connection.exec_driver_sql('ALTER TABLE '+TABLE+' ADD COLUMN '+name+' '+datatype)
    validate(connection)


def validate(connection):
    _validate(connection, stable=True)
