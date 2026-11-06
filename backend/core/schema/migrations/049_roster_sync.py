"""D4 additive roster configuration, preview receipts and external identity bindings."""
import importlib
import re
import uuid
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:049-roster-sync'
DDL = {
    'education_roster_connections': '''(
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
        provider VARCHAR(16) NOT NULL CHECK(provider IN ('google','canvas')),
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        config_encrypted TEXT NOT NULL,
        tokens_encrypted TEXT,
        created_by INTEGER,
        connected_by INTEGER,
        UNIQUE(org_id,provider), UNIQUE(id,org_id)
    )''',
    'education_roster_courses': '''(
        connection_id VARCHAR(36) NOT NULL,
        org_id INTEGER NOT NULL,
        course_id VARCHAR(255) NOT NULL,
        class_id VARCHAR(36) NOT NULL,
        PRIMARY KEY(connection_id,course_id),
        UNIQUE(class_id),
        FOREIGN KEY(connection_id,org_id) REFERENCES education_roster_connections(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(class_id,org_id) REFERENCES education_classes(id,org_id) ON DELETE RESTRICT
    )''',
    'education_roster_identities': '''(
        connection_id VARCHAR(36) NOT NULL,
        org_id INTEGER NOT NULL,
        subject VARCHAR(255) NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY(connection_id,subject),
        UNIQUE(connection_id,user_id),
        FOREIGN KEY(connection_id,org_id) REFERENCES education_roster_connections(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(user_id,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
    'education_roster_grant_links': '''(
        connection_id VARCHAR(36) NOT NULL,
        org_id INTEGER NOT NULL,
        subject VARCHAR(255) NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY(connection_id,subject),
        UNIQUE(connection_id,user_id),
        FOREIGN KEY(connection_id,org_id) REFERENCES education_roster_connections(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(user_id,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
    'education_roster_tombstones': '''(
        connection_id VARCHAR(36) NOT NULL REFERENCES education_roster_connections(id) ON DELETE RESTRICT,
        subject_hash VARCHAR(64) NOT NULL,
        PRIMARY KEY(connection_id,subject_hash)
    )''',
    'education_roster_receipts': '''(
        id VARCHAR(64) PRIMARY KEY NOT NULL,
        connection_id VARCHAR(36) NOT NULL REFERENCES education_roster_connections(id) ON DELETE RESTRICT,
        actor_id INTEGER,
        kind VARCHAR(16) NOT NULL CHECK(kind IN ('preview','oauth','exchange')),
        payload_encrypted TEXT NOT NULL,
        expires_at BIGINT NOT NULL
    )''',
}


def validate(connection):
    shape = importlib.import_module('core.schema.migrations.048_education_classes')._shape
    for table, ddl in DDL.items():
        name = '_roster_contract_' + uuid.uuid4().hex[:12]
        pg = connection.dialect.name == 'postgresql'
        reference = r',\s*FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\) ON DELETE RESTRICT'
        # Strip inline FKs too: PostgreSQL temporary tables cannot reference permanent tables.
        inline = r'(\w+) ([^,\n]+?) REFERENCES (\w+)\(([^)]+)\) ON DELETE RESTRICT'
        expected = re.sub(reference, '', ddl) if pg else ddl
        if pg:
            expected = re.sub(inline, r'\1 \2', expected)
        connection.exec_driver_sql(('CREATE TEMPORARY TABLE ' if pg else 'CREATE TABLE ') + name + expected)
        try:
            schema = connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one() if pg else None
            wanted = list(shape(connection, name, schema))
            if pg:
                wanted[3] = sorted(
                    [(tuple(c.strip() for c in cols.split(',')), target, tuple(keys.split(',')), 'RESTRICT')
                     for cols, target, keys in re.findall(reference, ddl)] +
                    [((col,), target, (key,), 'RESTRICT') for col, _, target, key in re.findall(inline, ddl)])
            if shape(connection, table) != tuple(wanted):
                raise MigrationError('Roster schema differs: ' + table)
            if pg and connection.execute(text('SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(:t) AND NOT convalidated'), {'t': table}).first():
                raise MigrationError('Roster constraints are not validated')
        finally:
            connection.exec_driver_sql('DROP TABLE ' + name)


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported roster dialect')
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    for required in ('education_classes', 'education_memberships'):
        if not inspect(connection).has_table(required):
            raise MigrationError('Roster migration requires ' + required)
    for table, ddl in DDL.items():
        if not inspect(connection).has_table(table):
            connection.exec_driver_sql('CREATE TABLE ' + table + ddl)
    validate(connection)
