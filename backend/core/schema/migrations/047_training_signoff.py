"""Add tenant-bound training requirements and current completion evidence."""
import uuid
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:047-training-signoff'
DDL = {
    'education_trainings': '''(
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        org_id INTEGER NOT NULL,
        cost_center_id INTEGER NOT NULL,
        school_wide BOOLEAN NOT NULL DEFAULT FALSE,
        name VARCHAR(160) NOT NULL,
        description TEXT NOT NULL,
        scope_kind VARCHAR(20) NOT NULL,
        scope_value VARCHAR(160) NOT NULL,
        quiz_json TEXT NOT NULL,
        pass_mark INTEGER NOT NULL,
        instructor_required BOOLEAN NOT NULL,
        expiry_days INTEGER,
        active BOOLEAN NOT NULL,
        revision INTEGER NOT NULL,
        edit_version INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(id,org_id),
        FOREIGN KEY(cost_center_id,org_id) REFERENCES education_cost_centers(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(created_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT,
        CHECK(scope_kind IN ('printer_group','machine_type','model')),
        CHECK(pass_mark BETWEEN 1 AND 100),
        CHECK(expiry_days IS NULL OR expiry_days BETWEEN 1 AND 3650),
        CHECK(revision > 0),
        CHECK(edit_version > 0)
    )''',
    'education_training_records': '''(
        training_id VARCHAR(36) NOT NULL,
        org_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        training_revision INTEGER NOT NULL,
        quiz_attempts INTEGER NOT NULL DEFAULT 0,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        cooldown_until TIMESTAMP,
        quiz_score INTEGER,
        quiz_passed_at TIMESTAMP,
        instructor_id INTEGER,
        instructor_at TIMESTAMP,
        completed_at TIMESTAMP,
        expires_at TIMESTAMP,
        revoked_at TIMESTAMP,
        PRIMARY KEY(training_id,user_id),
        FOREIGN KEY(training_id,org_id) REFERENCES education_trainings(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(user_id,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(instructor_id,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT,
        CHECK(training_revision > 0),
        CHECK(quiz_attempts >= 0 AND failed_attempts >= 0),
        CHECK(quiz_score IS NULL OR quiz_score BETWEEN 0 AND 100)
    )''',
}
COLUMNS = {
    'education_trainings': 'id org_id cost_center_id school_wide name description scope_kind scope_value quiz_json pass_mark instructor_required expiry_days active revision edit_version created_by created_at',
    'education_training_records': 'training_id org_id user_id training_revision quiz_attempts failed_attempts cooldown_until quiz_score quiz_passed_at instructor_id instructor_at completed_at expires_at revoked_at',
}


def _shape(connection, table, schema=None):
    i = inspect(connection)
    columns = [(c['name'], str(c['type']).upper(), c['nullable'],
                str(c.get('default')).lower()) for c in i.get_columns(table, schema=schema)]
    foreign = sorted((tuple(f['constrained_columns']), f['referred_table'],
                      tuple(f['referred_columns']), f['options'].get('ondelete'))
                     for f in i.get_foreign_keys(table, schema=schema))
    checks = sorted(''.join(c['sqltext'].lower().split()) for c in i.get_check_constraints(table, schema=schema))
    return (columns, i.get_pk_constraint(table, schema=schema)['constrained_columns'], foreign,
            sorted(tuple(u['column_names']) for u in i.get_unique_constraints(table, schema=schema)),
            [x for x in i.get_indexes(table, schema=schema) if not x.get('duplicates_constraint')], checks)


def validate(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported training dialect')
    postgres = connection.dialect.name == 'postgresql'
    for table, ddl in DDL.items():
        temporary = '_training_contract_' + uuid.uuid4().hex[:12]
        connection.exec_driver_sql('CREATE TABLE ' + temporary + ddl)
        try:
            schema = None
            if _shape(connection, table) != _shape(connection, temporary, schema):
                raise MigrationError('Training schema shape differs: ' + table)
            if postgres and connection.execute(text('SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(:table) AND NOT convalidated'), {'table': table}).first():
                raise MigrationError('Training constraints are not validated')
        finally:
            connection.exec_driver_sql('DROP TABLE ' + temporary)


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported training dialect')
    for table, ddl in DDL.items():
        if not inspect(connection).has_table(table):
            connection.exec_driver_sql('CREATE TABLE ' + table + ddl)
    validate(connection)
