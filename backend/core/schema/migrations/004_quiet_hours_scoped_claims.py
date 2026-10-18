"""Scope digest claims while conservatively preserving every historical claim."""
from sqlalchemy import inspect,text

MIGRATION_ID='python:004-quiet-hours-scoped-claims'
TABLES=('quiet_hours_digest_sends','quiet_hours_org_digest_sends')


def apply(connection):
    # sqlite3 legacy mode does not BEGIN for DDL. Force a real transaction
    # before CREATE so interruption cannot leave a replacement table behind.
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    for table in TABLES:
        # Identifiers are exclusively selected from the fixed table tuple.
        inspector=inspect(connection)
        columns={item['name'] for item in inspector.get_columns(table)}
        if 'scope_key' in columns:
            continue
        replacement=table+'_scoped_upgrade'
        identity='BIGSERIAL PRIMARY KEY' if connection.dialect.name=='postgresql' else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        user_column='user_id INTEGER NOT NULL,' if table==TABLES[0] else ''
        unique='user_id,scope_key,window_ended_at' if table==TABLES[0] else 'scope_key,window_ended_at'
        connection.execute(text(f'''CREATE TABLE {replacement} (
            id {identity}, {user_column} org_id INTEGER,
            scope_key TEXT NOT NULL, window_ended_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            delivery_status TEXT DEFAULT 'pending', UNIQUE({unique}))'''))
        names='id,user_id,org_id,window_ended_at,sent_at,delivery_status' if table==TABLES[0] else 'id,org_id,window_ended_at,sent_at,delivery_status'
        # Old per-user claims covered all scopes. Old NULL org claims may be
        # duplicated. Keep all rows, using distinct legacy keys for org rows.
        legacy="'legacy'" if table==TABLES[0] else "'legacy:' || CAST(id AS TEXT)"
        connection.execute(text(f'INSERT INTO {replacement} ({names},scope_key) SELECT {names},{legacy} FROM {table}'))
        connection.execute(text(f'DROP TABLE {table}'))
        connection.execute(text(f'ALTER TABLE {replacement} RENAME TO {table}'))
        prefix='qhds' if table==TABLES[0] else 'qhods'
        connection.execute(text(f'CREATE INDEX idx_{prefix}_window_ended_at ON {table}(window_ended_at)'))
        if table==TABLES[0]:
            connection.execute(text(f'CREATE INDEX idx_{prefix}_org_id ON {table}(org_id)'))
        if connection.dialect.name=='postgresql':
            connection.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {table}"))


def validate(connection):
    from core.schema.migrator import validate_column_shape
    inspector=inspect(connection)
    for table in TABLES:
        columns={item['name']:item for item in inspector.get_columns(table)}
        expected={'id','org_id','scope_key','window_ended_at','sent_at','delivery_status'}
        if table==TABLES[0]:expected.add('user_id')
        if set(columns)!=expected or columns['scope_key']['nullable']:
            raise RuntimeError('Scoped digest claim column shape mismatch')
        definitions={'org_id':'INTEGER','scope_key':'TEXT NOT NULL',
                     'window_ended_at':'TEXT NOT NULL',
                     'sent_at':'TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP',
                     'delivery_status':"TEXT DEFAULT 'pending'"}
        if table==TABLES[0]:definitions['user_id']='INTEGER NOT NULL'
        for name,definition in definitions.items():
            validate_column_shape(connection,table,name,definition)
        if inspector.get_pk_constraint(table).get('constrained_columns') != ['id']:
            raise RuntimeError('Scoped digest claim primary key is missing')
        expected_id='BIGINT' if connection.dialect.name=='postgresql' else 'INTEGER'
        if str(columns['id']['type']).upper()!=expected_id:
            raise RuntimeError('Scoped digest claim identity type mismatch')
        prefix='qhds' if table==TABLES[0] else 'qhods'
        required={f'idx_{prefix}_window_ended_at':('window_ended_at',)}
        if table==TABLES[0]:required[f'idx_{prefix}_org_id']=('org_id',)
        indexes={item['name']:(tuple(item['column_names']),bool(item['unique'])) for item in inspector.get_indexes(table)}
        for name,names in required.items():
            if indexes.get(name)!=(names,False):
                raise RuntimeError('Scoped digest claim index shape mismatch')
        unique=('user_id','scope_key','window_ended_at') if table==TABLES[0] else ('scope_key','window_ended_at')
        constraints={tuple(item['column_names']) for item in inspector.get_unique_constraints(table)}
        if constraints!={unique}:
            raise RuntimeError('Scoped digest claim uniqueness is missing')
