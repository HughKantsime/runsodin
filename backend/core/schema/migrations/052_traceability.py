"""E4: additive attempt receipts and tenant-scoped inspection policies."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:052-traceability'
# Dedicated number leaves the concurrently developed Enterprise features isolated.
DDL = {
    'traceability_records': '''(
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
        attempt INTEGER NOT NULL,
        snapshot TEXT NOT NULL,
        snapshot_sha256 VARCHAR(64) NOT NULL,
        captured_at VARCHAR(40) NOT NULL,
        inspection_result VARCHAR(4),
        inspection_notes TEXT,
        inspected_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        inspected_at VARCHAR(40),
        delivered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        delivered_at VARCHAR(40),
        PRIMARY KEY(job_id, attempt),
        CHECK(inspection_result IS NULL OR inspection_result IN ('pass','fail'))
    )''',
    'traceability_policies': '''(
        owner_id INTEGER NOT NULL,
        org_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
        target_kind VARCHAR(12) NOT NULL,
        target_key VARCHAR(100) NOT NULL,
        required INTEGER NOT NULL,
        PRIMARY KEY(owner_id, target_kind, target_key),
        CHECK(target_kind IN ('printer_tag','product')),
        CHECK(required IN (0,1)),
        CHECK((owner_id=0 AND org_id IS NULL) OR (owner_id>0 AND org_id IS NOT NULL AND org_id=owner_id))
    )''',
}


def _shape(connection, table):
    i = inspect(connection)
    foreign = sorted((tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']), f['options'].get('ondelete')) for f in i.get_foreign_keys(table))
    if connection.dialect.name == 'sqlite':
        foreign = sorted((r[3], r[2], r[4], r[6]) for r in connection.exec_driver_sql('PRAGMA foreign_key_list('+table+')'))
    return (
        [(c['name'], str(c['type']).upper(), c['nullable'], c.get('default')) for c in i.get_columns(table)],
        i.get_pk_constraint(table)['constrained_columns'],
        foreign,
        sorted(''.join(c['sqltext'].lower().split()).replace('::text', '').replace('::charactervarying', '') for c in i.get_check_constraints(table)),
        i.get_indexes(table), i.get_unique_constraints(table),
    )


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported traceability dialect')
    for table, ddl in DDL.items():
        if table not in inspect(connection).get_table_names():
            connection.exec_driver_sql('CREATE TABLE '+table+' '+ddl)
    validate(connection)


def validate(connection):
    # Compare against this migration's DDL on the same engine, including FK and checks.
    for table, ddl in DDL.items():
        name = '_validate_'+table
        connection.exec_driver_sql('CREATE TABLE '+name+' '+ddl)
        try:
            if _shape(connection, table) != _shape(connection, name):
                raise MigrationError('Traceability schema differs: '+table)
        finally:
            connection.exec_driver_sql('DROP TABLE '+name)
