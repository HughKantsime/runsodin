"""Retain Education tenant identities independently of current account membership."""
import re
from sqlalchemy import inspect, text

MIGRATION_ID = 'python:037-education-memberships'
PAIRS = {
    'education_cost_center_grants': ('user_id',),
    'education_upload_operations': ('user_id',),
    'education_submissions': ('submitted_by', 'approved_by'),
    'education_notification_outbox': ('recipient_user_id',),
    'education_rate_counters': ('user_id',),
    'education_storage_accounts': ('user_id',),
}


def validate(connection):
    inspector = inspect(connection)
    if not inspector.has_table('education_memberships'):
        raise RuntimeError('Retained Education memberships missing')
    if {c['name'] for c in inspector.get_columns('education_memberships')} != {'user_id', 'org_id', 'retained_at', 'ended_at'}:
        raise RuntimeError('Retained Education membership columns differ')
    if inspector.get_pk_constraint('education_memberships')['constrained_columns'] != ['user_id', 'org_id']:
        raise RuntimeError('Retained Education membership key differs')
    member_columns = {c['name']: c for c in inspector.get_columns('education_memberships')}
    if any(member_columns[name]['nullable'] for name in ('user_id', 'org_id', 'retained_at')) or not member_columns['ended_at']['nullable']:
        raise RuntimeError('Retained Education membership nullability differs')
    member_keys = {(tuple(k['constrained_columns']), k['referred_table'], tuple(k['referred_columns']), k['options'].get('ondelete')) for k in inspector.get_foreign_keys('education_memberships')}
    if member_keys != {(('user_id',), 'users', ('id',), 'RESTRICT'), (('org_id',), 'groups', ('id',), 'RESTRICT')}:
        raise RuntimeError('Retained Education membership foreign keys differ')
    if connection.dialect.name == 'sqlite':
        triggers = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars())
    else:
        triggers = set(connection.execute(text("SELECT trigger_name FROM information_schema.triggers WHERE trigger_schema=current_schema()")).scalars())
    for table, columns in PAIRS.items():
        for column in columns:
            name = f'trg_retained_{table}_{column}'
            required = {name + '_insert', name + '_update'} if connection.dialect.name == 'sqlite' else {name}
            if not required <= triggers:
                raise RuntimeError(f'Retained Education write triggers missing: {table}.{column}')
        keys = inspector.get_foreign_keys(table)
        if connection.dialect.name == 'sqlite':
            rows = connection.exec_driver_sql(f'PRAGMA foreign_key_list({table})').all()
            for column in columns:
                ids = {r[0] for r in rows if r[3] == column and r[2] == 'education_memberships'}
                if len(ids) != 1 or sorted((r[1], r[3], r[4], r[6]) for r in rows if r[0] in ids) != [(0, column, 'user_id', 'RESTRICT'), (1, 'org_id', 'org_id', 'RESTRICT')]:
                    raise RuntimeError(f'Retained Education enforced foreign key differs: {table}.{column}')
        for column in columns:
            matches = [k for k in keys if k['constrained_columns'] == [column, 'org_id']]
            if len(matches) != 1 or matches[0]['referred_table'] != 'education_memberships' or matches[0]['referred_columns'] != ['user_id', 'org_id'] or matches[0]['options'].get('ondelete') != 'RESTRICT':
                raise RuntimeError(f'Retained Education foreign key differs: {table}.{column}')


def apply(connection):
    if inspect(connection).has_table('education_memberships'):
        validate(connection)
        return
    if connection.dialect.name == 'postgresql':
        connection.execute(text('LOCK TABLE users, ' + ', '.join(PAIRS) + ' IN ACCESS EXCLUSIVE MODE'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — table and column names come from the PAIRS constant
    sources = ['SELECT id AS user_id, group_id AS org_id FROM users WHERE group_id IS NOT NULL']
    sources += [f'SELECT {col} AS user_id, org_id FROM {table} WHERE {col} IS NOT NULL' for table, cols in PAIRS.items() for col in cols]
    pairs = ' UNION '.join(sources)
    orphan = connection.execute(text('SELECT 1 FROM (' + pairs + ') p LEFT JOIN users u ON u.id=p.user_id AND u.group_id=p.org_id LEFT JOIN groups g ON g.id=p.org_id WHERE u.id IS NULL OR g.id IS NULL LIMIT 1')).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — table and column names come from the PAIRS constant
    if orphan:
        raise RuntimeError('Orphan Education membership; migration refused')
    connection.execute(text('''CREATE TABLE education_memberships (
        user_id INTEGER NOT NULL,
        org_id INTEGER NOT NULL,
        retained_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP NULL,
        PRIMARY KEY(user_id, org_id),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
        FOREIGN KEY(org_id) REFERENCES groups(id) ON DELETE RESTRICT
    )'''))
    connection.execute(text('INSERT INTO education_memberships(user_id,org_id) ' + pairs))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — table and column names come from the PAIRS constant
    for table, columns in PAIRS.items():
        if connection.dialect.name == 'postgresql':
            for fk in inspect(connection).get_foreign_keys(table):
                if fk['constrained_columns'] in [[col, 'org_id'] for col in columns]:
                    name = connection.dialect.identifier_preparer.quote(fk['name'])
                    connection.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT {name}'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — table/column names from PAIRS; constraint names from inspection
            for col in columns:
                name = f'fk_{table}_{col}_retained'
                connection.execute(text(f'ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({col},org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT NOT VALID'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — table/column names from PAIRS
                connection.execute(text(f'ALTER TABLE {table} VALIDATE CONSTRAINT {name}'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — constraint name built from PAIRS
        else:
            original = connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {'name': table}).scalar_one()
            changed, count = re.subn(r'REFERENCES\s+users\s*\(\s*id\s*,\s*group_id\s*\)', 'REFERENCES education_memberships(user_id, org_id)', original, flags=re.I)
            if count != len(columns):
                raise RuntimeError(f'Unexpected Education ownership schema: {table}')
            sequence = connection.execute(text('SELECT seq FROM sqlite_sequence WHERE name=:name'), {'name': table}).scalar() if 'AUTOINCREMENT' in original.upper() else None
            extras = connection.execute(text("SELECT sql FROM sqlite_master WHERE tbl_name=:name AND type IN ('index','trigger') AND sql IS NOT NULL"), {'name': table}).scalars().all()
            temp = '__retained_' + table
            changed = re.sub(r'^(CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?' + table + r'["`\]]?', lambda m: m[1] + temp, changed, count=1, flags=re.I)
            connection.exec_driver_sql(changed)
            names = ','.join('"' + c['name'] + '"' for c in inspect(connection).get_columns(table))
            # Closed destinations: schema rebuilding must never become a generic
            # data-copy writer. Only column identifiers come from reflection.
            copies = {
                'education_cost_center_grants': 'INSERT INTO __retained_education_cost_center_grants ({names}) SELECT {names} FROM education_cost_center_grants',
                'education_upload_operations': 'INSERT INTO __retained_education_upload_operations ({names}) SELECT {names} FROM education_upload_operations',
                'education_submissions': 'INSERT INTO __retained_education_submissions ({names}) SELECT {names} FROM education_submissions',
                'education_notification_outbox': 'INSERT INTO __retained_education_notification_outbox ({names}) SELECT {names} FROM education_notification_outbox',
                'education_rate_counters': 'INSERT INTO __retained_education_rate_counters ({names}) SELECT {names} FROM education_rate_counters',
                'education_storage_accounts': 'INSERT INTO __retained_education_storage_accounts ({names}) SELECT {names} FROM education_storage_accounts',
            }
            connection.exec_driver_sql(copies[table].format(names=names))
            connection.exec_driver_sql(f'DROP TABLE {table}')
            connection.exec_driver_sql(f'ALTER TABLE {temp} RENAME TO {table}')
            if sequence is not None:
                connection.execute(text('UPDATE sqlite_sequence SET seq=:seq WHERE name=:name AND seq<:seq'), {'seq': sequence, 'name': table})
            for ddl in extras:
                connection.exec_driver_sql(ddl)
    _install_retention_triggers(connection)
    validate(connection)


def _retention_insert(column):
    return (f'INSERT INTO education_memberships(user_id,org_id) '
            f'SELECT id,group_id FROM users WHERE id=NEW.{column} '
            'AND group_id=NEW.org_id ON CONFLICT(user_id,org_id) DO NOTHING;')


def sqlite_triggers():
    return {f'trg_retained_{table}_{column}_{event.lower()}':
            f'CREATE TRIGGER trg_retained_{table}_{column}_{event.lower()} BEFORE {event} ON {table} BEGIN {_retention_insert(column)} END'
            for table, columns in PAIRS.items() for column in columns for event in ('INSERT', 'UPDATE')}


def pg_functions():
    return {f'trg_retained_{table}_{column}': f' BEGIN {_retention_insert(column)} RETURN NEW; END '
            for table, columns in PAIRS.items() for column in columns}


def pg_triggers():
    return {(table, f'trg_retained_{table}_{column}'):
            f'CREATE TRIGGER trg_retained_{table}_{column} BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION trg_retained_{table}_{column}()'
            for table, columns in PAIRS.items() for column in columns}


def _install_retention_triggers(connection):
    # Covers every first dependent write, including reconciliation and outbox.
    if connection.dialect.name == 'sqlite':
        for ddl in sqlite_triggers().values():
            connection.exec_driver_sql(ddl)
    else:
        for name, body in pg_functions().items():
            connection.exec_driver_sql(f'CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $$'+body+'$$')
        for ddl in pg_triggers().values():
            connection.exec_driver_sql(ddl.replace('EXECUTE FUNCTION ', 'EXECUTE FUNCTION public.'))
