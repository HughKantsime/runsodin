"""Add controlled-part policies; no changes to existing work or network policy."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:054-controlled-parts'
TABLES = {'controlled_files': ('file_id', 'print_files'), 'controlled_jobs': ('job_id', 'jobs')}
FIELDS = {'label': 'VARCHAR(80)', 'audience_id': 'INTEGER', 'printer_tag': 'VARCHAR(100)',
          'retention_days': 'INTEGER', 'reason': 'VARCHAR(500)', 'changed_by': 'INTEGER',
          'marked_at': 'VARCHAR(40)', 'purge_previewed_at': 'VARCHAR(40)'}


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported controlled parts dialect')
    from core.controlled_audience_schema import create_audiences
    create_audiences(connection)
    for table, (key, parent) in TABLES.items():
        if table not in inspect(connection).get_table_names():
            connection.exec_driver_sql(f'''CREATE TABLE {table} (
                {key} INTEGER PRIMARY KEY NOT NULL,
                label VARCHAR(80) NOT NULL, audience_id INTEGER NOT NULL,
                printer_tag VARCHAR(100) NOT NULL, retention_days INTEGER NOT NULL,
                reason VARCHAR(500) NOT NULL, changed_by INTEGER,
                marked_at VARCHAR(40) NOT NULL, purge_previewed_at VARCHAR(40),
                FOREIGN KEY ({key}) REFERENCES {parent}(id) ON DELETE RESTRICT,
                FOREIGN KEY (audience_id) REFERENCES controlled_audiences(id) ON DELETE RESTRICT,
                FOREIGN KEY (changed_by) REFERENCES users(id) ON DELETE SET NULL,
                CHECK (retention_days BETWEEN 1 AND 3650)
            )''')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    from core.controlled_audience_schema import validate_audiences
    validate_audiences(connection)
    for table, (key, parent) in TABLES.items():
        expected = {key: 'INTEGER', **FIELDS}
        columns = {c['name']: c for c in inspector.get_columns(table)}
        if set(columns) != set(expected) or any(str(columns[n]['type']).upper() != t or
                columns[n]['nullable'] != (n in ('changed_by', 'purge_previewed_at')) or columns[n].get('default') is not None
                for n, t in expected.items()):
            raise MigrationError('Controlled parts column shape differs')
        if inspector.get_pk_constraint(table)['constrained_columns'] != [key]:
            raise MigrationError('Controlled parts primary key differs')
        foreign = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']),
                    f['options'].get('ondelete')) for f in inspector.get_foreign_keys(table)}
        if foreign != {((key,), parent, ('id',), 'RESTRICT'),
                       (('audience_id',), 'controlled_audiences', ('id',), 'RESTRICT'),
                       (('changed_by',), 'users', ('id',), 'SET NULL')}:
            raise MigrationError('Controlled parts foreign keys differ')
        checks = inspector.get_check_constraints(table)
        from core.schema.bootstrap import _normalize_check_definition
        def canonical(value):
            value = _normalize_check_definition(value, connection.dialect.name)
            return ' '.join(value.replace('(', '').replace(')', '').lower().split())
        normalized = [canonical(c['sqltext']) for c in checks]
        if len(normalized) != 1 or normalized[0] not in ('retention_days between 1 and 3650', 'retention_days >= 1 and retention_days <= 3650'):
            raise MigrationError('Controlled parts retention constraint differs')
        if inspector.get_indexes(table) or inspector.get_unique_constraints(table):
            raise MigrationError('Controlled parts constraints differ')
