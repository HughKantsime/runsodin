"""Prospective spool/attempt births, retained across source deletion. No backfill."""
import importlib
import re
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID='python:022-economic-source-lifetimes'
TABLES={'spools':'spool','print_jobs':'attempt'}
HISTORY='cost_source_lifetimes'

# Canonical PostgreSQL16 deparser forms from owned native creation receipt.
PG_CHECKS={'cost_source_lifetimes_birth_nonce_check': "CHECK ((birth_nonce ~ '^[0-9a-f]{32}$'::text))", 'cost_source_lifetimes_check': "CHECK ((((state = 'unresolved'::text) AND (identity_uid IS NULL) AND (owner_scope IS NULL) AND (owner_key IS NULL) AND (job_uid IS NULL) AND (order_uid IS NULL) AND (item_uid IS NULL) AND (attempt_uid IS NULL)) OR ((state = 'known'::text) AND (identity_uid IS NOT NULL) AND (owner_scope IS NOT NULL) AND (owner_key IS NOT NULL) AND (((owner_scope = 'global'::text) AND (owner_key = 0)) OR ((owner_scope = 'org'::text) AND (owner_key > 0))) AND (((resource_kind = 'spool'::text) AND (job_uid IS NULL) AND (order_uid IS NULL) AND (item_uid IS NULL) AND (attempt_uid IS NULL)) OR ((resource_kind = 'attempt'::text) AND (job_uid IS NOT NULL) AND (order_uid IS NOT NULL) AND (item_uid IS NOT NULL) AND (attempt_uid IS NOT NULL) AND (attempt_uid = identity_uid))))))", 'cost_source_lifetimes_resource_id_check': 'CHECK ((resource_id > 0))', 'cost_source_lifetimes_resource_kind_check': "CHECK ((resource_kind = ANY (ARRAY['spool'::text, 'attempt'::text])))", 'cost_source_lifetimes_state_check': "CHECK ((state = ANY (ARRAY['known'::text, 'unresolved'::text])))"}


def _normal(sql):
    return re.sub(r'\s+',' ',sql).strip().rstrip(';')


def _history_ddl(postgres):
    nonce="birth_nonce ~ '^[0-9a-f]{32}$'" if postgres else "length(birth_nonce)=32 AND birth_nonce NOT GLOB '*[^0-9a-f]*'"
    return f'''CREATE TABLE {HISTORY} (
 birth_nonce TEXT PRIMARY KEY NOT NULL CHECK({nonce}),
 resource_kind TEXT NOT NULL CHECK(resource_kind IN ('spool','attempt')),
 resource_id INTEGER NOT NULL CHECK(resource_id>0),
 state TEXT NOT NULL CHECK(state IN ('known','unresolved')),
 identity_uid TEXT UNIQUE,
 owner_scope TEXT,
 owner_key INTEGER,
 job_uid TEXT,
 order_uid TEXT,
 item_uid TEXT,
 attempt_uid TEXT,
 CHECK((state='unresolved' AND identity_uid IS NULL AND owner_scope IS NULL AND owner_key IS NULL AND job_uid IS NULL AND order_uid IS NULL AND item_uid IS NULL AND attempt_uid IS NULL)
 OR (state='known' AND identity_uid IS NOT NULL AND owner_scope IS NOT NULL AND owner_key IS NOT NULL AND ((owner_scope='global' AND owner_key=0) OR (owner_scope='org' AND owner_key>0)) AND ((resource_kind='spool' AND job_uid IS NULL AND order_uid IS NULL AND item_uid IS NULL AND attempt_uid IS NULL) OR (resource_kind='attempt' AND job_uid IS NOT NULL AND order_uid IS NOT NULL AND item_uid IS NOT NULL AND attempt_uid IS NOT NULL AND attempt_uid=identity_uid)))),
 FOREIGN KEY(identity_uid,resource_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(attempt_uid,owner_scope,owner_key) REFERENCES cost_attempt_bindings(attempt_uid,owner_scope,owner_key) ON DELETE RESTRICT
)'''


def _lifetime_proof(n='NEW'):
    spool=f"EXISTS(SELECT 1 FROM spools s WHERE s.id={n}.resource_id AND s.economic_birth_nonce={n}.birth_nonce AND s.economic_uid IS NULL AND ({n}.state='unresolved' OR ({n}.owner_scope=CASE WHEN s.org_id IS NULL THEN 'global' ELSE 'org' END AND {n}.owner_key=COALESCE(s.org_id,0))))"
    attempt=f"EXISTS(SELECT 1 FROM print_jobs p WHERE p.id={n}.resource_id AND p.economic_birth_nonce={n}.birth_nonce AND p.economic_uid IS NULL AND ({n}.state='unresolved' OR (p.spool_snapshot IS NULL AND EXISTS(SELECT 1 FROM jobs j JOIN cost_attempt_bindings b ON b.attempt_uid={n}.identity_uid WHERE j.id=p.scheduled_job_id AND j.economic_uid={n}.job_uid AND b.job_uid={n}.job_uid AND b.order_uid={n}.order_uid AND b.item_uid={n}.item_uid AND b.owner_scope={n}.owner_scope AND b.owner_key={n}.owner_key AND {n}.owner_scope=CASE WHEN j.charged_to_org_id IS NULL THEN 'global' ELSE 'org' END AND {n}.owner_key=COALESCE(j.charged_to_org_id,0)))))"
    registry=f"({n}.state='unresolved' OR EXISTS(SELECT 1 FROM cost_identities r WHERE r.identity_uid={n}.identity_uid AND r.resource_kind={n}.resource_kind AND r.resource_id={n}.resource_id AND r.owner_scope={n}.owner_scope AND r.owner_key={n}.owner_key))"
    return f"{registry} AND (({n}.resource_kind='spool' AND {spool}) OR ({n}.resource_kind='attempt' AND {attempt}))"


def _source_proof(table,n='NEW'):
    kind=TABLES[table]
    owner=(f"l.owner_scope=CASE WHEN {n}.org_id IS NULL THEN 'global' ELSE 'org' END AND l.owner_key=COALESCE({n}.org_id,0)") if kind=='spool' else f"{n}.spool_snapshot IS NULL AND EXISTS(SELECT 1 FROM jobs j JOIN cost_attempt_bindings b ON b.attempt_uid=l.identity_uid WHERE j.id={n}.scheduled_job_id AND j.economic_uid=l.job_uid AND b.job_uid=l.job_uid AND b.order_uid=l.order_uid AND b.item_uid=l.item_uid AND b.owner_scope=l.owner_scope AND b.owner_key=l.owner_key)"
    return f"EXISTS(SELECT 1 FROM {HISTORY} l WHERE l.birth_nonce={n}.economic_birth_nonce AND l.resource_kind='{kind}' AND l.resource_id={n}.id AND l.state='known' AND l.identity_uid={n}.economic_uid AND {owner})"


def _sqlite_triggers():
    result={}
    for table,kind in TABLES.items():
        insert=f"NEW.economic_uid IS NOT NULL OR (NEW.economic_birth_nonce IS NOT NULL AND EXISTS(SELECT 1 FROM {HISTORY} WHERE birth_nonce=NEW.economic_birth_nonce)) OR EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id AND (economic_uid IS NOT NULL OR economic_birth_nonce IS NOT NULL))"
        columns=['id','economic_uid','org_id'] if kind=='spool' else ['id','economic_uid','scheduled_job_id','printer_id']
        immutable=' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in columns)
        update=f"NEW.economic_birth_nonce IS NOT OLD.economic_birth_nonce OR (OLD.economic_birth_nonce IS NOT NULL AND NEW.id IS NOT OLD.id) OR (OLD.economic_uid IS NOT NULL AND ({immutable})) OR (NEW.economic_uid IS NOT NULL AND NEW.economic_uid IS NOT OLD.economic_uid AND NOT {_source_proof(table)})"
        for suffix,event,condition in [('insert','INSERT',insert),('update','UPDATE',update)]:
            name=table+'_economic_birth_'+suffix
            result[name]=f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN SELECT CASE WHEN {condition} THEN RAISE(ABORT,'economic source birth differs') END; END"
    result['cost_source_lifetimes_insert']=f"CREATE TRIGGER cost_source_lifetimes_insert BEFORE INSERT ON {HISTORY} BEGIN SELECT CASE WHEN EXISTS(SELECT 1 FROM {HISTORY} WHERE birth_nonce=NEW.birth_nonce OR (NEW.identity_uid IS NOT NULL AND identity_uid=NEW.identity_uid)) OR NOT ({_lifetime_proof()}) THEN RAISE(ABORT,'economic lifetime source differs') END; END"
    for event in ('UPDATE','DELETE'):
        name='cost_source_lifetimes_no_'+event.lower()
        result[name]=f"CREATE TRIGGER {name} BEFORE {event} ON {HISTORY} BEGIN SELECT RAISE(ABORT,'economic lifetime is immutable'); END"
    return result


def _pg_functions():
    result={}
    for table,kind in TABLES.items():
        columns=['id','economic_uid','org_id'] if kind=='spool' else ['id','economic_uid','scheduled_job_id','printer_id']
        immutable=' OR '.join(f'NEW.{c} IS DISTINCT FROM OLD.{c}' for c in columns)
        result[table+'_economic_birth_guard']=f'''BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.economic_uid IS NOT NULL OR (NEW.economic_birth_nonce IS NOT NULL AND EXISTS(SELECT 1 FROM {HISTORY} WHERE birth_nonce=NEW.economic_birth_nonce)) THEN RAISE EXCEPTION 'economic source birth differs' USING ERRCODE='23514'; END IF;
 ELSE
  IF NEW.economic_birth_nonce IS DISTINCT FROM OLD.economic_birth_nonce OR (OLD.economic_birth_nonce IS NOT NULL AND NEW.id IS DISTINCT FROM OLD.id) OR (OLD.economic_uid IS NOT NULL AND ({immutable})) OR (NEW.economic_uid IS NOT NULL AND NEW.economic_uid IS DISTINCT FROM OLD.economic_uid AND NOT {_source_proof(table)}) THEN RAISE EXCEPTION 'economic source birth differs' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END;'''
    result['cost_source_lifetimes_guard']=f'''BEGIN
 IF TG_OP!='INSERT' THEN RAISE EXCEPTION 'economic lifetime is immutable' USING ERRCODE='23514'; END IF;
 IF NOT ({_lifetime_proof()}) THEN RAISE EXCEPTION 'economic lifetime source differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;'''
    return result


def _pg_triggers():
    result={}
    for table in TABLES:
        name=table+'_economic_birth_guard'
        result[(table,name)]=f'CREATE TRIGGER {name} BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION {name}()'
    result[(HISTORY,'cost_source_lifetimes_guard')]=f'CREATE TRIGGER cost_source_lifetimes_guard BEFORE INSERT OR DELETE OR UPDATE ON public.{HISTORY} FOR EACH ROW EXECUTE FUNCTION cost_source_lifetimes_guard()'
    return result


def validate(connection):
    inspector=inspect(connection)
    postgres=connection.dialect.name=='postgresql'
    expected_columns={'birth_nonce','resource_kind','resource_id','state','identity_uid','owner_scope','owner_key','job_uid','order_uid','item_uid','attempt_uid'}
    columns=inspector.get_columns(HISTORY)
    if {c['name'] for c in columns}!=expected_columns or any(c.get('default') is not None for c in columns):
        raise MigrationError('Economic lifetime table shape differs')
    for c in columns:
        expected_type='INTEGER' if c['name'] in ('resource_id','owner_key') else 'TEXT'
        required=c['name'] in ('birth_nonce','resource_kind','resource_id','state')
        if str(c['type']).upper()!=expected_type or bool(c['nullable'])==required:
            raise MigrationError('Economic lifetime column shape differs')
    if inspector.get_pk_constraint(HISTORY)['constrained_columns']!=['birth_nonce']:
        raise MigrationError('Economic lifetime primary key differs')
    if {tuple(c['column_names']) for c in inspector.get_unique_constraints(HISTORY)}!={('identity_uid',)}:
        raise MigrationError('Economic lifetime identity key differs')
    expected_fks={
        ('identity_uid','resource_kind','owner_scope','owner_key'):('cost_identities',('identity_uid','resource_kind','owner_scope','owner_key')),
        ('attempt_uid','owner_scope','owner_key'):('cost_attempt_bindings',('attempt_uid','owner_scope','owner_key')),
    }
    actual_fks=inspector.get_foreign_keys(HISTORY)
    if len(actual_fks)!=2 or {tuple(f['constrained_columns']):(f['referred_table'],tuple(f['referred_columns'])) for f in actual_fks}!=expected_fks or (postgres and any(f.get('referred_schema') not in (None,'public') or f.get('options',{}).get('ondelete')!='RESTRICT' for f in actual_fks)):
        raise MigrationError('Economic lifetime typed foreign keys differ')
    if postgres:
        checks=dict(connection.execute(text("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"),{'table':HISTORY}).all())
        if checks!=PG_CHECKS:raise MigrationError('Economic lifetime native CHECK shape differs')
        rows=connection.execute(text("SELECT c.convalidated,c.condeferrable,c.condeferred,p.relnamespace=t.relnamespace,c.confmatchtype,c.confupdtype,c.confdeltype FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_class p ON p.oid=c.confrelid WHERE c.conrelid=to_regclass(:table) AND c.contype='f'"),{'table':HISTORY}).all()
        if sorted(tuple(r) for r in rows)!=[(True,False,False,True,'s','a','r')]*2:raise MigrationError('Economic lifetime native FK enforcement differs')
    for table in TABLES:
        columns={c['name']:c for c in inspector.get_columns(table)}
        for name in ('economic_uid','economic_birth_nonce'):
            c=columns.get(name)
            if c is None or str(c['type']).upper()!='TEXT' or not c['nullable'] or c.get('default') is not None:
                raise MigrationError('Economic birth column shape differs')
            indices=[i for i in inspector.get_indexes(table) if name in (i.get('column_names') or [])]
            if len(indices)!=1 or indices[0]['name']!=table+'_'+name+'_unique' or indices[0]['column_names']!=[name] or not indices[0]['unique']:
                raise MigrationError('Economic birth unique index differs')
            fks=[f for f in inspector.get_foreign_keys(table) if name in f['constrained_columns']]
            referred='cost_identities' if name=='economic_uid' else HISTORY
            target='identity_uid' if name=='economic_uid' else 'birth_nonce'
            if not postgres:
                physical=[r for r in connection.exec_driver_sql(f'PRAGMA foreign_key_list({table})').all() if r[3]==name]
                if len(physical)!=1 or physical[0][6]!='RESTRICT':raise MigrationError('Economic birth foreign enforcement differs')
                declaration=connection.execute(text("SELECT sql FROM sqlite_master WHERE name=:name AND type='table'"),{'name':table}).scalar_one()
                parts=importlib.import_module('core.schema.migrations.021_economic_identity')._sqlite_declarations(declaration)
                expected=f'{name} TEXT REFERENCES {referred}({target}) ON DELETE RESTRICT'+(' DEFERRABLE INITIALLY DEFERRED' if name=='economic_birth_nonce' else '')
                if [_normal(v) for v in parts if re.match(r'^'+name+r'\s',v)]!=[_normal(expected)]:raise MigrationError('Economic birth deferred declaration differs')
                if fks:fks[0]['options']={'ondelete':'RESTRICT','deferrable':True,'initially':'DEFERRED'}
            if len(fks)!=1 or fks[0]['constrained_columns']!=[name] or fks[0]['referred_table']!=referred or fks[0]['referred_columns']!=[target] or fks[0].get('options',{}).get('ondelete')!='RESTRICT':
                raise MigrationError('Economic birth foreign key differs')
            options=fks[0].get('options',{})
            if name=='economic_birth_nonce' and (not options.get('deferrable') or options.get('initially')!='DEFERRED'):
                raise MigrationError('Economic birth deferred enforcement differs')
            if postgres:
                rows=connection.execute(text("SELECT c.convalidated,c.condeferrable,c.condeferred,c.confrelid=to_regclass(:target),c.confmatchtype,c.confupdtype,c.confdeltype FROM pg_constraint c WHERE c.conrelid=to_regclass(:table) AND c.contype='f' AND (SELECT attnum FROM pg_attribute WHERE attrelid=c.conrelid AND attname=:column)=ANY(c.conkey)"),{'table':table,'column':name,'target':'public.'+referred}).all()
                deferred=name=='economic_birth_nonce'
                if [tuple(r) for r in rows]!=[(True,deferred,deferred,True,'s','a','r')]:raise MigrationError('Economic source native FK enforcement differs')
                flags=connection.execute(text("SELECT indisvalid,indisready,indimmediate,indnullsnotdistinct,indpred IS NULL,indexprs IS NULL,indnkeyatts=indnatts FROM pg_index WHERE indexrelid=to_regclass(:name)"),{'name':table+'_'+name+'_unique'}).all()
                if [tuple(r) for r in flags]!=[(True,True,True,False,True,True,True)]:raise MigrationError('Economic source native index enforcement differs')
            else:
                index_sql=connection.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),{'name':table+'_'+name+'_unique'}).scalar_one()
                if _normal(index_sql)!=f'CREATE UNIQUE INDEX {table}_{name}_unique ON {table}({name})':raise MigrationError('Economic source unique SQL differs')
    if postgres:
        schema=connection.execute(text('SELECT current_schema()')).scalar_one()
        if schema!='public':raise MigrationError('Economic birth requires canonical public schema')
        actual=connection.execute(text("SELECT p.proname,p.prosrc,p.proconfig,p.prosecdef,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prorettype::regtype::text,p.pronargs,l.lanname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname LIKE '%economic_birth_guard' OR n.nspname='public' AND p.proname='cost_source_lifetimes_guard' ORDER BY p.proname,p.oid")).all()
        expected=[(name,body,['search_path=pg_catalog, public'],False,'v','u',False,False,'trigger',0,'plpgsql') for name,body in sorted(_pg_functions().items())]
        if [tuple(r) for r in actual]!=expected:raise MigrationError('Economic birth function shape differs')
        rows=connection.execute(text("SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid),t.tgenabled,f.pronamespace=c.relnamespace FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_proc f ON f.oid=t.tgfoid WHERE NOT t.tgisinternal AND c.relnamespace=to_regnamespace('public') AND (t.tgname LIKE '%_economic_birth_%' OR t.tgname LIKE 'cost_source_lifetimes_%')")).all()
        if {(r[0],r[1]):(r[2],r[3],r[4]) for r in rows}!={key:(sql,'O',True) for key,sql in _pg_triggers().items()}:
            raise MigrationError('Economic birth trigger shape differs')
    else:
        stored=connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),{'name':HISTORY}).scalar_one()
        if _normal(stored)!=_normal(_history_ddl(False)):raise MigrationError('Economic lifetime check/FK shape differs')
        actual=dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (name LIKE '%_economic_birth_%' OR name LIKE 'cost_source_lifetimes_%')")).all())
        if {k:_normal(v) for k,v in actual.items()}!={k:_normal(v) for k,v in _sqlite_triggers().items()}:
            raise MigrationError('Economic birth trigger shape differs')
        if connection.exec_driver_sql('PRAGMA foreign_key_check').first():raise MigrationError('Economic birth foreign data differs')
    for table,kind in TABLES.items():
        bad=connection.execute(text(f"SELECT s.id FROM {table} s LEFT JOIN {HISTORY} l ON l.birth_nonce=s.economic_birth_nonce WHERE (s.economic_birth_nonce IS NOT NULL AND (l.birth_nonce IS NULL OR l.resource_kind!=:kind OR l.resource_id!=s.id)) OR (s.economic_uid IS NOT NULL AND (l.birth_nonce IS NULL OR l.state!='known' OR l.identity_uid IS DISTINCT FROM s.economic_uid)) LIMIT 1"),{'kind':kind}).first()
        if bad:raise MigrationError('Economic birth association differs')


def apply(connection):
    importlib.import_module('core.schema.migrations.009_cost_foundation')._lock(connection)
    inspector=inspect(connection)
    if not set(TABLES).issubset(inspector.get_table_names()):raise MigrationError('Economic birth source tables missing')
    footprint=inspector.has_table(HISTORY) or any({'economic_uid','economic_birth_nonce'} & {c['name'] for c in inspector.get_columns(t)} for t in TABLES)
    if connection.dialect.name=='sqlite':
        footprint=footprint or connection.execute(text("SELECT 1 FROM sqlite_master WHERE name LIKE '%_economic_birth_%' OR name LIKE 'cost_source_lifetimes_%' LIMIT 1")).first() is not None
    else:
        footprint=footprint or connection.execute(text("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() AND (p.proname LIKE '%_economic_birth_%' OR p.proname LIKE 'cost_source_lifetimes_%') LIMIT 1")).first() is not None
    if footprint:
        validate(connection);return
    postgres=connection.dialect.name=='postgresql'
    if connection.dialect.name not in ('sqlite','postgresql'):raise MigrationError('Unsupported economic birth dialect')
    connection.exec_driver_sql(_history_ddl(postgres))
    for table in TABLES:
        connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN economic_uid TEXT REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT')
        connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN economic_birth_nonce TEXT REFERENCES {HISTORY}(birth_nonce) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED')
        for name in ('economic_uid','economic_birth_nonce'):
            connection.exec_driver_sql(f'CREATE UNIQUE INDEX {table}_{name}_unique ON {table}({name})')
    if postgres:
        for name,body in _pg_functions().items():
            connection.exec_driver_sql(f'CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $$'+body+'$$')
        for statement in _pg_triggers().values():connection.exec_driver_sql(statement)
    else:
        for statement in _sqlite_triggers().values():connection.exec_driver_sql(statement)
    validate(connection)
