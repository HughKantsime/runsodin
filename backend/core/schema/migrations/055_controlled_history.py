"""Retain deleted-job audiences and policy clocks without mutating audit history."""
import re
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:055-controlled-history'
TABLES = {
    'job_id_high_water': {'singleton': 'INTEGER', 'high_water': 'INTEGER'},
    'controlled_job_tombstones': {
        'job_id': 'INTEGER', 'audience_id': 'INTEGER', 'label': 'VARCHAR(80)',
        'printer_tag': 'VARCHAR(100)', 'retention_days': 'INTEGER',
        'marked_at': 'VARCHAR(40)', 'created_at': 'VARCHAR(40)', 'audit_max_id': 'INTEGER',
        'rule_index': 'INTEGER',
        'purge_previewed_at': 'VARCHAR(40)', 'retention_initial_days': 'INTEGER',
        'retention_changes': 'TEXT'},
    'controlled_retention_clocks': {
        'resource_type': 'VARCHAR(10)', 'resource_id': 'INTEGER',
        'marked_at': 'VARCHAR(40)', 'initial_days': 'INTEGER', 'changes': 'TEXT',
        'released_at': 'VARCHAR(40)'},
}
NULLABLE = {'released_at', 'purge_previewed_at', 'retention_initial_days', 'retention_changes'}
KEYS = {'job_id_high_water': ['singleton'],
        'controlled_job_tombstones': ['job_id', 'audience_id', 'audit_max_id', 'rule_index'],
        'controlled_retention_clocks': ['resource_type', 'resource_id']}
FOREIGN = {'controlled_job_tombstones': [
    (('audience_id',), 'controlled_audiences', ('id',), 'RESTRICT')]}


def largest_referenced_job_id(connection):
    # Discover declared FKs as well as legacy links without FK constraints.
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    queries = ['SELECT 0 AS id']
    for table in inspector.get_table_names():
        columns = {c['name'] for c in inspector.get_columns(table)}
        links = columns & {'job_id', 'scheduled_job_id'}
        if table == 'jobs':
            links.add('id')
        if table == 'timelapses':
            links.add('print_job_id')  # historical name, actually jobs.id
        for fk in inspector.get_foreign_keys(table):
            if fk['referred_table'] == 'jobs':
                links.update(fk['constrained_columns'])
        if table == 'print_jobs':
            # Printer protocol identifier (TEXT), not an ODIN jobs.id link.
            # scheduled_job_id is the ODIN reference, even when job_id is numeric.
            links.discard('job_id')
        for column in sorted(links):
            queries.append(f'SELECT {quote(column)} AS id FROM {quote(table)}')
        if {'entity_type', 'entity_id'} <= columns:
            queries.append(f"SELECT entity_id AS id FROM {quote(table)} WHERE entity_type='job'")
        for kind in ('resource_type', 'resource_kind'):
            if {kind, 'resource_id'} <= columns:
                queries.append(f"SELECT resource_id AS id FROM {quote(table)} WHERE {kind} IN ('job','jobs')")
    # SQLite MAX sorts TEXT above INTEGER. Normalize before aggregating: only
    # integer storage values and nonempty ASCII-digit text are job identifiers.
    # GLOB alone stops at NUL, so explicitly reject embedded NULs as well.
    identifier = 'id'
    if connection.dialect.name == 'sqlite':
        identifier = ("CASE WHEN typeof(id)='integer' THEN id "
                      "WHEN typeof(id)='text' AND id<>'' AND id NOT GLOB '*[^0-9]*' "
                      "AND instr(id,char(0))=0 THEN CAST(id AS INTEGER) END")
    return connection.exec_driver_sql(
        f'SELECT COALESCE(max({identifier}),0) FROM (' + ' UNION ALL '.join(queries) + ')').scalar_one()


def initialize_job_ids(connection):
    if connection.dialect.name != 'sqlite':
        return
    if connection.exec_driver_sql(
            'SELECT high_water FROM job_id_high_water WHERE singleton=1').scalar_one_or_none() is None:
        existing = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'jobs_high_water_%'").all()
        if existing:
            raise MigrationError(
                'Job identity high-water row is missing while job identity triggers remain. '
                'Stop ODIN and recover the all-time high_water from a trusted backup or exported '
                'job history (including deleted IDs), then run sqlite3 <database> '
                '"INSERT INTO job_id_high_water(singleton,high_water) VALUES(1,<verified_high_water>);" '
                'and restart ODIN. Referenced IDs alone cannot safely rebuild this row.')
    connection.exec_driver_sql('INSERT INTO job_id_high_water(singleton,high_water) VALUES(1,?) '
                              'ON CONFLICT(singleton) DO UPDATE SET '
                              'high_water=max(high_water,excluded.high_water)',
                              (largest_referenced_job_id(connection),))
    # Cover ORM, bulk, retention and raw deletion, including explicit imported ids.
    for statement in job_id_triggers().values():
        connection.exec_driver_sql(statement.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))


def job_id_triggers():
    triggers = {f'jobs_high_water_{action.lower()}':
            f'CREATE TRIGGER jobs_high_water_{action.lower()} AFTER {action} ON jobs '
            f'BEGIN UPDATE job_id_high_water SET high_water=max(high_water,{row}.id) WHERE singleton=1; END'
            for action, row in (('INSERT', 'NEW'), ('DELETE', 'OLD'))}
    triggers.update({
        'jobs_high_water_prevent_delete':
            'CREATE TRIGGER jobs_high_water_prevent_delete BEFORE DELETE ON job_id_high_water '
            "BEGIN SELECT RAISE(ABORT, 'Job identity high-water row cannot be deleted'); END",
        'jobs_high_water_prevent_lower':
            'CREATE TRIGGER jobs_high_water_prevent_lower BEFORE UPDATE ON job_id_high_water '
            'WHEN NEW.high_water < OLD.high_water '
            "BEGIN SELECT RAISE(ABORT, 'Job identity high-water cannot be lowered'); END",
    })
    return triggers


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported controlled history dialect')
    if not {'controlled_audiences', 'controlled_jobs', 'controlled_files'} <= set(inspect(connection).get_table_names()):
        raise MigrationError('Controlled parts schema required')
    for table, fields in TABLES.items():
        columns = ','.join(name + ' ' + kind + ('' if name in NULLABLE else ' NOT NULL')
                           for name, kind in fields.items())
        foreign = ''.join(', FOREIGN KEY (' + ','.join(columns) + ') REFERENCES ' + target
                          + '(' + ','.join(referred) + ') ON DELETE ' + deletion
                          for columns, target, referred, deletion in FOREIGN.get(table, []))
        check = ', CHECK (singleton=1 AND high_water>=0)' if table == 'job_id_high_water' else ''
        connection.exec_driver_sql(f"CREATE TABLE IF NOT EXISTS {table} ({columns}, PRIMARY KEY ({','.join(KEYS[table])}){foreign}{check})")
    initialize_job_ids(connection)
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    for table, fields in TABLES.items():
        columns = {c['name']: c for c in inspector.get_columns(table)}
        if set(columns) != set(fields) or any(str(columns[n]['type']).upper() != kind
                or columns[n]['nullable'] != (n in NULLABLE) or columns[n].get('default') is not None
                for n, kind in fields.items()):
            raise MigrationError('Controlled history column shape differs')
        if inspector.get_pk_constraint(table)['constrained_columns'] != KEYS[table]:
            raise MigrationError('Controlled history primary key differs')
        foreign = inspector.get_foreign_keys(table)
        actual = {(tuple(f['constrained_columns']), f['referred_table'],
                   tuple(f['referred_columns']), f['options'].get('ondelete')) for f in foreign}
        if actual != set(FOREIGN.get(table, [])):
            raise MigrationError('Controlled history foreign key differs')
        from core.schema.bootstrap import _normalize_check_definition
        def canonical(value):
            normalized = _normalize_check_definition(value, connection.dialect.name)
            if connection.dialect.name == 'postgresql':
                normalized = re.sub(r'::(?:character varying|text)\b', '', normalized)
            return ''.join(normalized.replace('(', '').replace(')', '').lower().split())
        checks = [canonical(c['sqltext']) for c in inspector.get_check_constraints(table)]
        expected = [canonical('singleton=1 AND high_water>=0')] if table == 'job_id_high_water' else []
        if (inspector.get_indexes(table) or inspector.get_unique_constraints(table) or checks != expected):
            raise MigrationError('Controlled history constraints differ')

    if connection.dialect.name == 'sqlite':
        rows = connection.exec_driver_sql('SELECT singleton,high_water FROM job_id_high_water').all()
        if (len(rows) != 1 or rows[0][0] != 1
                or type(rows[0][1]) is not int or rows[0][1] < 0):
            raise MigrationError('Job identity high-water row missing or invalid: '
                                 'expected singleton=1 and a nonnegative integer high_water')
        actual = dict(connection.exec_driver_sql(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'jobs_high_water_%'").all())
        normalize = lambda sql: ''.join(sql.lower().split())
        if {n: normalize(s) for n, s in actual.items()} != {n: normalize(s) for n, s in job_id_triggers().items()}:
            raise MigrationError('Job identity high-water trigger differs')
