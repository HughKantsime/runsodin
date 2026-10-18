"""Prospective source identity columns. No historical allocation or ORM drift."""
import importlib
import re
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:021-economic-identity'
TABLES = {'orders': ('order', ['org_id']), 'order_items': ('item', ['order_id']),
          'jobs': ('job', ['order_item_id', 'charged_to_org_id'])}


def _normal(value):
    return re.sub(r'\s+', ' ', value).strip().rstrip(';')



def _sqlite_declarations(ddl):
    body=ddl[ddl.index('(')+1:ddl.rindex(')')]
    parts=[];start=0;depth=0;quoted=None;i=0
    while i<len(body):
        char=body[i]
        if quoted:
            if char==quoted:
                if i+1<len(body) and body[i+1]==quoted:i+=1
                else:quoted=None
        elif char in ("'", '"', '`'):quoted=char
        elif char=='(':depth+=1
        elif char==')':depth-=1
        elif char==',' and depth==0:parts.append(body[start:i].strip());start=i+1
        i+=1
    parts.append(body[start:].strip())
    return parts

def _association(table, alias='NEW'):
    kind = TABLES[table][0]
    n = alias
    base = f"EXISTS(SELECT 1 FROM cost_identities r WHERE r.identity_uid={n}.economic_uid AND r.resource_kind='{kind}' AND r.resource_id={n}.id"
    if table == 'orders':
        return base + f" AND r.owner_scope=CASE WHEN {n}.org_id IS NULL THEN 'global' ELSE 'org' END AND r.owner_key=COALESCE({n}.org_id,0))"
    owner = f"r.owner_scope=CASE WHEN {n}.charged_to_org_id IS NULL THEN 'global' ELSE 'org' END AND r.owner_key=COALESCE({n}.charged_to_org_id,0)"
    if table == 'order_items':
        return base + f" AND EXISTS(SELECT 1 FROM orders o JOIN cost_order_memberships m ON m.order_uid=o.economic_uid WHERE o.id={n}.order_id AND m.child_uid={n}.economic_uid AND m.child_kind='item' AND m.owner_scope=r.owner_scope AND m.owner_key=r.owner_key AND r.owner_scope=CASE WHEN o.org_id IS NULL THEN 'global' ELSE 'org' END AND r.owner_key=COALESCE(o.org_id,0)))"
    return base + f" AND {owner} AND (({n}.order_item_id IS NULL AND NOT EXISTS(SELECT 1 FROM cost_order_memberships m WHERE m.child_uid={n}.economic_uid) AND NOT EXISTS(SELECT 1 FROM cost_job_bindings b WHERE b.job_uid={n}.economic_uid)) OR EXISTS(SELECT 1 FROM order_items i JOIN orders o ON o.id=i.order_id JOIN cost_job_bindings b ON b.order_uid=o.economic_uid AND b.item_uid=i.economic_uid JOIN cost_order_memberships m ON m.child_uid={n}.economic_uid AND m.child_kind='job' AND m.order_uid=o.economic_uid WHERE i.id={n}.order_item_id AND b.job_uid={n}.economic_uid AND b.owner_scope=r.owner_scope AND b.owner_key=r.owner_key AND m.owner_scope=r.owner_scope AND m.owner_key=r.owner_key AND r.owner_scope=CASE WHEN o.org_id IS NULL THEN 'global' ELSE 'org' END AND r.owner_key=COALESCE(o.org_id,0))))"


def _sqlite_triggers(table):
    immutable = ' OR '.join(f'NEW.{col} IS NOT OLD.{col}' for col in ['id', 'economic_uid', *TABLES[table][1]])
    return {
        table+'_economic_insert': f"CREATE TRIGGER {table}_economic_insert BEFORE INSERT ON {table} WHEN NEW.economic_uid IS NOT NULL OR EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id AND economic_uid IS NOT NULL) BEGIN SELECT RAISE(ABORT,'economic source creation must be fresh NULL'); END",
        table+'_economic_update': f"CREATE TRIGGER {table}_economic_update BEFORE UPDATE ON {table} WHEN (OLD.economic_uid IS NOT NULL AND ({immutable})) OR (NEW.economic_uid IS NOT NULL AND NOT {_association(table)}) BEGIN SELECT RAISE(ABORT,'economic source anchor differs'); END",
    }


def _pg_body(table):
    immutable = ' OR '.join(f'NEW.{col} IS DISTINCT FROM OLD.{col}' for col in ['id', 'economic_uid', *TABLES[table][1]])
    return f"""BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.economic_uid IS NOT NULL THEN RAISE EXCEPTION 'economic source creation must be fresh NULL' USING ERRCODE='23514'; END IF;
 ELSE
  IF (OLD.economic_uid IS NOT NULL AND ({immutable})) OR (NEW.economic_uid IS NOT NULL AND NOT {_association(table)}) THEN RAISE EXCEPTION 'economic source anchor differs' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;"""


def _validate_data(connection):
    for table in TABLES:
        invalid = connection.execute(text(f'SELECT id FROM {table} AS source WHERE economic_uid IS NOT NULL AND NOT {_association(table, "source")} LIMIT 1')).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
        if invalid:
            raise MigrationError('Economic source association differs')


def validate(connection):
    inspector = inspect(connection)
    for table in TABLES:
        columns = {c['name']: c for c in inspector.get_columns(table)}
        c = columns.get('economic_uid')
        if c is None or str(c['type']).upper() != 'TEXT' or not c['nullable'] or c.get('default') is not None or c.get('identity') or c.get('computed'):
            raise MigrationError('Economic column shape differs')
        if any('economic_uid' in u['column_names'] for u in inspector.get_unique_constraints(table)):
            raise MigrationError('Economic unexpected UID constraint')
        indices = [i for i in inspector.get_indexes(table) if 'economic_uid' in i['column_names']]
        expected_name = table+'_economic_uid_unique'
        if len(indices) != 1 or indices[0]['name'] != expected_name or not indices[0]['unique'] or indices[0]['column_names'] != ['economic_uid'] or indices[0].get('dialect_options', {}).get(connection.dialect.name+'_where') is not None:
            raise MigrationError('Economic index shape differs')
        foreign = [f for f in inspector.get_foreign_keys(table) if 'economic_uid' in f['constrained_columns']]
        if len(foreign) != 1:
            raise MigrationError('Economic FK shape differs')
        fk = foreign[0]
        options = dict(fk.get('options', {}))
        if connection.dialect.name == 'sqlite':
            physical = [r for r in connection.exec_driver_sql(f'PRAGMA foreign_key_list({table})').all() if r[3]=='economic_uid']
            if len(physical)!=1 or physical[0][5]!='NO ACTION' or physical[0][6]!='RESTRICT':
                raise MigrationError('Economic SQLite FK enforcement differs')
            options['ondelete'] = physical[0][6]
        if fk['constrained_columns'] != ['economic_uid'] or fk['referred_table'] != 'cost_identities' or fk['referred_columns'] != ['identity_uid'] or fk.get('referred_schema') not in (None, inspector.default_schema_name) or options.get('ondelete') != 'RESTRICT' or options.get('onupdate') not in (None, 'NO ACTION') or options.get('deferrable') or options.get('initially'):
            raise MigrationError('Economic FK authority differs')
        if connection.dialect.name == 'sqlite':
            ddl = connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {'name':table}).scalar_one()
            declarations = [p for p in _sqlite_declarations(ddl) if re.match(r'^economic_uid\s',p)]
            if len(declarations)!=1 or _normal(declarations[0]) != 'economic_uid TEXT REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT':
                raise MigrationError('Economic SQLite raw column declaration differs')
            index_sql = connection.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"), {'name':expected_name}).scalar_one()
            if _normal(index_sql) != f'CREATE UNIQUE INDEX {expected_name} ON {table}(economic_uid)':
                raise MigrationError('Economic SQLite raw index shape differs')
            triggers = dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:table AND name LIKE :prefix"), {'table': table, 'prefix': table+'_economic_%'}).all())
            if {k:_normal(v) for k,v in triggers.items()} != {k:_normal(v) for k,v in _sqlite_triggers(table).items()}:
                raise MigrationError('Economic guard shape differs')
            if connection.exec_driver_sql(f"PRAGMA foreign_key_check('{table}')").first():
                raise MigrationError('Economic foreign data differs')
        else:
            schema = connection.execute(text('SELECT current_schema()')).scalar_one()
            functions = connection.execute(text("""SELECT p.prosrc,p.proconfig,p.prosecdef,p.provolatile,l.lanname,p.prorettype::regtype::text,p.pronargs FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname=:schema AND p.proname=:name"""), {'schema': schema, 'name': table+'_economic_guard'}).all()
            expected_config = ['search_path=pg_catalog, '+connection.dialect.identifier_preparer.quote(schema)]
            if len(functions) != 1 or _normal(functions[0][0]) != _normal(_pg_body(table)) or tuple(functions[0][1:]) != (expected_config, False, 'v', 'plpgsql', 'trigger', 0):
                raise MigrationError('Economic native function shape differs')
            triggers = connection.execute(text("""SELECT t.tgname,t.tgtype,t.tgenabled,p.proname,p.pronamespace=c.relnamespace,t.tgqual IS NULL,t.tgnargs,t.tgattr::text FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_proc p ON p.oid=t.tgfoid WHERE t.tgrelid=to_regclass(:table) AND NOT t.tgisinternal AND t.tgname LIKE :prefix"""), {'table': table, 'prefix': table+'_economic_%'}).all()
            if [tuple(r) for r in triggers] != [(table+'_economic_guard',23,'O',table+'_economic_guard',True,True,0,'')]:
                raise MigrationError('Economic native guard shape differs')
            rows = connection.execute(text("""SELECT i.indisvalid,i.indisready,i.indimmediate,i.indnullsnotdistinct,i.indpred IS NULL,i.indexprs IS NULL,i.indnkeyatts=i.indnatts FROM pg_index i WHERE i.indexrelid=to_regclass(:name)"""), {'name': expected_name}).all()
            if [tuple(r) for r in rows] != [(True,True,True,False,True,True,True)]:
                raise MigrationError('Economic native unique enforcement differs')
            rows = connection.execute(text("""SELECT c.convalidated,c.condeferrable,p.relnamespace=t.relnamespace FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_class p ON p.oid=c.confrelid WHERE c.conrelid=to_regclass(:table) AND c.conname=:name"""), {'table': table, 'name':table+'_economic_uid_fkey'}).all()
            if [tuple(r) for r in rows] != [(True,False,True)]:
                raise MigrationError('Economic native FK enforcement differs')
    _validate_data(connection)


def apply(connection):
    foundation = importlib.import_module('core.schema.migrations.009_cost_foundation')
    foundation._lock(connection)
    foundation.validate(connection)
    inspector = inspect(connection)
    if not set(TABLES).issubset(inspector.get_table_names()):
        raise MigrationError('Economic source tables missing')
    footprint = any('economic_uid' in {c['name'] for c in inspector.get_columns(t)} for t in TABLES)
    if connection.dialect.name == 'sqlite':
        footprint = footprint or connection.execute(text("SELECT 1 FROM sqlite_master WHERE name LIKE '%_economic_%' LIMIT 1")).first() is not None
    else:
        footprint = footprint or connection.execute(text("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() AND p.proname LIKE '%_economic_%' UNION ALL SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relname LIKE '%_economic_%' LIMIT 1")).first() is not None
    if footprint:
        validate(connection)
        return
    for table in TABLES:
        constraint = '' if connection.dialect.name == 'sqlite' else f' CONSTRAINT {table}_economic_uid_fkey'
        connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN economic_uid TEXT{constraint} REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT')
        connection.exec_driver_sql(f'CREATE UNIQUE INDEX {table}_economic_uid_unique ON {table}(economic_uid)')
        if connection.dialect.name == 'sqlite':
            for ddl in _sqlite_triggers(table).values():
                connection.exec_driver_sql(ddl)
        else:
            schema = connection.execute(text('SELECT current_schema()')).scalar_one()
            quoted = connection.dialect.identifier_preparer.quote(schema)
            connection.exec_driver_sql(f'CREATE FUNCTION {table}_economic_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, {quoted} AS $$'+_pg_body(table)+'$$')
            connection.exec_driver_sql(f'CREATE TRIGGER {table}_economic_guard BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {table}_economic_guard()')
    validate(connection)
