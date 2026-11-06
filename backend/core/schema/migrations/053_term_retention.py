"""Add opt-in term policies and durable content cleanup receipts; retain history."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:053-term-retention'
COLUMNS = {
    'education_erasure_pending': {'user_id': 'INTEGER', 'identifiers_json': 'TEXT'},
    'education_term_policies': {
        'id': 'VARCHAR(36)', 'org_id': 'INTEGER', 'cost_center_id': 'INTEGER',
        'name': 'VARCHAR(120)', 'starts_at': 'VARCHAR(32)', 'ends_at': 'VARCHAR(32)',
        'files_days': 'INTEGER', 'timelapses_days': 'INTEGER',
        'thumbnails_days': 'INTEGER', 'portfolio_days': 'INTEGER',
        'created_by': 'INTEGER', 'created_at': 'VARCHAR(32)',
    },
    'education_term_cleanup': {
        'term_id': 'VARCHAR(36)', 'submission_id': 'INTEGER', 'kind': 'VARCHAR(16)',
        'state': 'VARCHAR(16)', 'updated_at': 'VARCHAR(32)',
    },
}
NULLABLE = {'files_days', 'timelapses_days', 'thumbnails_days', 'portfolio_days', 'created_by'}
KEYS = {'education_erasure_pending': ['user_id'], 'education_term_policies': ['id'], 'education_term_cleanup': ['term_id', 'submission_id', 'kind']}
FOREIGN = {
    'education_erasure_pending': {(( 'user_id',), 'users', ('id',), 'RESTRICT')},
    'education_term_policies': {(('org_id',), 'groups', ('id',), 'RESTRICT'),
        (('cost_center_id', 'org_id'), 'education_cost_centers', ('id', 'org_id'), 'RESTRICT'),
        (('created_by',), 'users', ('id',), 'SET NULL')},
    'education_term_cleanup': {(('term_id',), 'education_term_policies', ('id',), 'RESTRICT'),
        (('submission_id',), 'education_submissions', ('id',), 'RESTRICT')},
}


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported term retention dialect')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS education_term_policies (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        org_id INTEGER NOT NULL,
        cost_center_id INTEGER NOT NULL,
        name VARCHAR(120) NOT NULL,
        starts_at VARCHAR(32) NOT NULL, ends_at VARCHAR(32) NOT NULL,
        files_days INTEGER, timelapses_days INTEGER, thumbnails_days INTEGER, portfolio_days INTEGER,
        created_by INTEGER,
        created_at VARCHAR(32) NOT NULL,
        FOREIGN KEY(org_id) REFERENCES groups(id) ON DELETE RESTRICT,
        FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
        FOREIGN KEY(cost_center_id,org_id) REFERENCES education_cost_centers(id,org_id) ON DELETE RESTRICT)''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS education_term_cleanup (
        term_id VARCHAR(36) NOT NULL,
        submission_id INTEGER NOT NULL,
        kind VARCHAR(16) NOT NULL, state VARCHAR(16) NOT NULL, updated_at VARCHAR(32) NOT NULL,
        PRIMARY KEY(term_id,submission_id,kind),
        FOREIGN KEY(term_id) REFERENCES education_term_policies(id) ON DELETE RESTRICT,
        FOREIGN KEY(submission_id) REFERENCES education_submissions(id) ON DELETE RESTRICT)''')
    connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS education_erasure_pending (
        user_id INTEGER NOT NULL PRIMARY KEY, identifiers_json TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT)''')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    for table, expected in COLUMNS.items():
        if not inspector.has_table(table):
            raise MigrationError('Term retention table missing')
        actual = {c['name']: c for c in inspector.get_columns(table)}
        if set(actual) != set(expected) or any(
            str(actual[name]['type']).upper() != kind or
            actual[name]['nullable'] != (name in NULLABLE) or actual[name].get('default') is not None
            for name, kind in expected.items()
        ):
            raise MigrationError('Term retention column shape differs')
        if inspector.get_pk_constraint(table)['constrained_columns'] != KEYS[table]:
            raise MigrationError('Term retention primary key differs')
        foreign = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']),
                    f['options'].get('ondelete')) for f in inspector.get_foreign_keys(table)}
        if foreign != FOREIGN[table] or inspector.get_indexes(table) or inspector.get_unique_constraints(table) or inspector.get_check_constraints(table):
            raise MigrationError('Term retention constraints differ')
