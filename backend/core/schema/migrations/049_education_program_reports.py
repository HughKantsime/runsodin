"""D3: class reporting marks and approved instructor effort; additive after D2/037."""
import re
import uuid
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:049-education-program-reports'
DDL = {
    'education_program_marks': '''(
        class_id VARCHAR(36) NOT NULL,
        org_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        participant BOOLEAN NOT NULL DEFAULT FALSE,
        concentrator BOOLEAN NOT NULL DEFAULT FALSE,
        marked_by INTEGER,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(class_id,user_id),
        CHECK(NOT concentrator OR participant),
        FOREIGN KEY(class_id,org_id) REFERENCES education_classes(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(user_id,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(marked_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
    'education_instructor_hours': '''(
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        class_id VARCHAR(36) NOT NULL,
        org_id INTEGER NOT NULL,
        instructor_id INTEGER NOT NULL,
        activity_date VARCHAR(10) NOT NULL,
        hours FLOAT NOT NULL CHECK(hours>=0 AND hours<=24),
        approved_by INTEGER,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(class_id,instructor_id,activity_date),
        FOREIGN KEY(class_id,org_id) REFERENCES education_classes(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(instructor_id,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(approved_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
}
COLUMNS = {
    'education_program_marks': 'class_id org_id user_id participant concentrator marked_by revision updated_at',
    'education_instructor_hours': 'id class_id org_id instructor_id activity_date hours approved_by revision updated_at',
}


def _shape(connection, table, schema=None):
    i = inspect(connection)
    def normalized(value):
        # Formatting/casts may differ between dialects; quoted values must not.
        parts = re.split(r"('(?:''|[^'])*')", str(value))
        return ''.join(part if index % 2 else re.sub(
            r"::(?:text|character varying)|[\s()]", '', part).lower()
            for index, part in enumerate(parts))
    return (
        [(c['name'], str(c['type']), c['nullable'], normalized(c.get('default')))
         for c in i.get_columns(table, schema=schema)],
        i.get_pk_constraint(table, schema=schema)['constrained_columns'],
        sorted(tuple(c['column_names']) for c in i.get_unique_constraints(table, schema=schema)),
        sorted((tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']),
                f['options'].get('ondelete')) for f in i.get_foreign_keys(table, schema=schema)),
        sorted(normalized(c['sqltext']) for c in i.get_check_constraints(table, schema=schema)),
        sorted((tuple(x['column_names']), x['unique']) for x in i.get_indexes(table, schema=schema)
               if not x.get('duplicates_constraint')),
    )


def validate(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported program reports dialect')
    for table, ddl in DDL.items():
        name = '_program_contract_' + uuid.uuid4().hex[:12]
        pg = connection.dialect.name == 'postgresql'
        # As in039, PostgreSQL temp tables cannot reference permanent tables.
        reference = r',\s*FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\) ON DELETE RESTRICT'
        expected_ddl = re.sub(reference, '', ddl) if pg else ddl
        connection.exec_driver_sql(('CREATE TEMPORARY TABLE ' if pg else 'CREATE TABLE ') + name + expected_ddl)
        try:
            schema = connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one() if pg else None
            wanted = list(_shape(connection, name, schema))
            if pg:
                wanted[3] = sorted((tuple(c.strip() for c in columns.split(',')), target,
                                    tuple(c.strip() for c in keys.split(',')), 'RESTRICT')
                                   for columns, target, keys in re.findall(reference, ddl))
            if _shape(connection, table) != tuple(wanted):
                raise MigrationError('Program reports schema differs: ' + table)
            if pg and connection.execute(text('SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(:t) AND NOT convalidated'), {'t': table}).first():
                raise MigrationError('Program reports constraints are not validated')
        finally:
            connection.exec_driver_sql('DROP TABLE ' + name)


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported program reports dialect')
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    for prerequisite in ('education_classes', 'education_memberships'):
        if not inspect(connection).has_table(prerequisite):
            raise MigrationError('Program reports requires ' + prerequisite)
    for table, ddl in DDL.items():
        if not inspect(connection).has_table(table):
            connection.exec_driver_sql('CREATE TABLE ' + table + ddl)
    validate(connection)
