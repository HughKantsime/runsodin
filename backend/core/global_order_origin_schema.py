"""Unregistered026 prospective global order provenance. Caller owns transaction."""
import importlib
import re
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

SOURCE = 'economic_global_origin_nonce'
ORIGINS = 'cost_global_order_origins'
EXCLUSIONS = 'cost_global_origin_exclusions'
TABLES = (ORIGINS, EXCLUSIONS)
INDEX = 'orders_global_origin_nonce_unique'

def normal(value):
    return ''.join(x if x.startswith("'") else re.sub(r'\s+', '', x).lower()
                   for x in re.findall(r"'(?:''|[^'])*'|[^']+", str(value))).rstrip(';')

def nonce_check(name, pg):
    return f"{name} ~ '^[0-9a-f]{{32}}$'" if pg else f"typeof({name})='text' AND length({name})=32 AND {name} NOT GLOB '*[^0-9a-f]*'"

def ddl(table, pg, *, reference=False):
    origin = table == ORIGINS
    timestamp = "TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('UTC',CURRENT_TIMESTAMP)" if pg else 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'
    extra = f",origin_nonce TEXT NOT NULL UNIQUE CONSTRAINT go_nonce CHECK({nonce_check('origin_nonce', pg)}),proof_kind TEXT NOT NULL CONSTRAINT go_proof CHECK(proof_kind='verified_platform_admin_global_creation'),recorded_at {timestamp}" if origin else ''
    timecheck = 'isfinite(recorded_at)' if pg else "typeof(recorded_at)='text' AND length(recorded_at)=19 AND strftime('%Y-%m-%d %H:%M:%S',recorded_at)=recorded_at"
    if origin: extra += f',CONSTRAINT go_time CHECK({timecheck})'
    ownercheck = "resource_kind='order' AND owner_scope='global' AND owner_key=0" + (" AND typeof(owner_key)='integer'" if not pg else '')
    foreign = '' if reference else ',CONSTRAINT go_identity FOREIGN KEY(order_uid,resource_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON UPDATE RESTRICT ON DELETE RESTRICT'
    name = 'global_origin_reference' if reference else table
    return f"CREATE {'TEMPORARY ' if reference else ''}TABLE {name}(order_uid TEXT PRIMARY KEY NOT NULL,resource_kind TEXT NOT NULL,owner_scope TEXT NOT NULL,owner_key INTEGER NOT NULL{extra},CONSTRAINT go_owner CHECK({ownercheck}){foreign})"

def registry(n='NEW'):
    return f"EXISTS(SELECT 1 FROM cost_identities r WHERE r.identity_uid={n}.order_uid AND r.resource_kind='order' AND r.owner_scope='global' AND r.owner_key=0)"

def admission(table):
    other = EXCLUSIONS if table == ORIGINS else ORIGINS
    base = f"{registry()} AND NOT EXISTS(SELECT 1 FROM {other} WHERE order_uid=NEW.order_uid) AND NOT EXISTS(SELECT 1 FROM {table} WHERE order_uid=NEW.order_uid)"
    if table == ORIGINS:
        base += f" AND NOT EXISTS(SELECT 1 FROM {ORIGINS} WHERE origin_nonce=NEW.origin_nonce) AND EXISTS(SELECT 1 FROM orders s JOIN cost_identities r ON r.identity_uid=s.economic_uid WHERE s.economic_uid=NEW.order_uid AND s.{SOURCE}=NEW.origin_nonce AND s.org_id IS NULL AND r.resource_id=s.id AND r.resource_kind='order' AND r.owner_scope='global' AND r.owner_key=0)"
    return base

def source_insert(pg):
    return f"EXISTS(SELECT 1 FROM orders WHERE id=NEW.id) OR NEW.economic_uid IS NOT NULL OR (NEW.{SOURCE} IS NOT NULL AND (NEW.org_id IS NOT NULL OR NOT ({nonce_check('NEW.'+SOURCE, pg)}) OR EXISTS(SELECT 1 FROM {ORIGINS} WHERE origin_nonce=NEW.{SOURCE})))"

def sqlite_triggers():
    result = {
        'orders_global_origin_insert': f"CREATE TRIGGER orders_global_origin_insert BEFORE INSERT ON orders BEGIN SELECT CASE WHEN {source_insert(False)} THEN RAISE(ABORT,'global source must be fresh') END; END",
        'orders_global_origin_update': f"CREATE TRIGGER orders_global_origin_update BEFORE UPDATE ON orders WHEN NEW.{SOURCE} IS NOT OLD.{SOURCE} OR (NEW.{SOURCE} IS NOT NULL AND NEW.org_id IS NOT NULL) BEGIN SELECT RAISE(ABORT,'global source nonce is immutable'); END",
    }
    for table in TABLES:
        result[table+'_insert'] = f"CREATE TRIGGER {table}_insert BEFORE INSERT ON {table} BEGIN SELECT CASE WHEN NOT ({admission(table)}) THEN RAISE(ABORT,'global provenance differs') END; END"
        for action in ('UPDATE', 'DELETE'):
            name = table+'_no_'+action.lower()
            result[name] = f"CREATE TRIGGER {name} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'global provenance is immutable'); END"
    return result

def pg_functions():
    result = {'orders_global_origin_guard': f"BEGIN IF TG_OP='INSERT' THEN IF {source_insert(True)} THEN RAISE EXCEPTION 'global source must be fresh' USING ERRCODE='23514'; END IF; ELSE IF NEW.{SOURCE} IS DISTINCT FROM OLD.{SOURCE} OR (NEW.{SOURCE} IS NOT NULL AND NEW.org_id IS NOT NULL) THEN RAISE EXCEPTION 'global source nonce is immutable' USING ERRCODE='23514'; END IF; END IF; RETURN NEW; END"}
    for table in TABLES:
        result[table+'_guard'] = f"BEGIN IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'global provenance is immutable' USING ERRCODE='23514'; END IF; PERFORM 1 FROM public.cost_identities WHERE identity_uid=NEW.order_uid FOR UPDATE NOWAIT; IF NOT ({admission(table)}) THEN RAISE EXCEPTION 'global provenance differs' USING ERRCODE='23514'; END IF; RETURN NEW; END"
    return result

def pg_triggers():
    result = {('orders','orders_global_origin_guard'): 'CREATE TRIGGER orders_global_origin_guard BEFORE INSERT OR UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION orders_global_origin_guard()'}
    for table in TABLES:
        result[(table,table+'_guard')] = f'CREATE TRIGGER {table}_guard BEFORE INSERT OR DELETE OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION {table}_guard()'
        result[(table,table+'_no_truncate')] = f'CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION {table}_guard()'
    return result

def boundary(c):
    if c.dialect.name == 'postgresql':
        if c.connection.driver_connection.autocommit: raise MigrationError('Global provenance transaction required')
        if c.execute(text('SELECT current_schema()')).scalar_one() != 'public': raise MigrationError('Global provenance schema differs')
        setting = int(c.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one())
        if setting == 0 or setting > 2000: c.execute(text("SELECT set_config('lock_timeout','2000',true)"))
        for table in ('orders','cost_identities',*TABLES):
            oid, public = c.execute(text('SELECT to_regclass(:n)::oid,to_regclass(:p)::oid'), {'n':table,'p':'public.'+table}).one()
            if oid != public: raise MigrationError('Global provenance relation shadowed')
            if oid is not None:
                if c.execute(text('SELECT relkind,relpersistence FROM pg_class WHERE oid=:oid'),{'oid':oid}).one() != ('r','p'): raise MigrationError('Global provenance relation differs')
                c.exec_driver_sql('LOCK TABLE public.'+table+' IN SHARE ROW EXCLUSIVE MODE NOWAIT')
    elif c.dialect.name == 'sqlite':
        driver = c.connection.driver_connection
        if driver.execute('PRAGMA ignore_check_constraints').fetchone()[0] != 0: raise MigrationError('Global provenance CHECK enforcement required')
        if driver.execute('PRAGMA foreign_keys').fetchone()[0] != 1: raise MigrationError('Global provenance foreign keys required')
        if not driver.in_transaction: c.exec_driver_sql('BEGIN IMMEDIATE')
        if c.execute(text("SELECT 1 FROM sqlite_temp_master WHERE name IN ('orders','cost_identities','cost_global_order_origins','cost_global_origin_exclusions')")).first(): raise MigrationError('Global provenance relation shadowed')
        c.exec_driver_sql('UPDATE orders SET id=id WHERE 0')
    else: raise MigrationError('Unsupported global provenance dialect')

def source_keys(c, *, installed):
    i = inspect(c)
    if i.get_pk_constraint('orders')['constrained_columns'] != ['id'] or i.get_unique_constraints('orders'): raise MigrationError('Global source unique routes differ')
    unique = [(x['name'],tuple(x['column_names'])) for x in i.get_indexes('orders') if x['unique']]
    wanted = [('orders_economic_uid_unique',('economic_uid',))]
    if installed: wanted.append((INDEX,(SOURCE,)))
    if sorted(unique) != sorted(wanted): raise MigrationError('Global source unique routes differ')
    for x in i.get_indexes('orders'):
        if x['unique'] and x.get('dialect_options',{}).get(c.dialect.name+'_where') is not None: raise MigrationError('Global source partial key differs')

def _pg_foreign(c, table):
    rows=c.execute(text("""SELECT k.confrelid,n.nspname,p.relname,
    ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY x(num,pos) JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.num ORDER BY x.pos),
    ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY x(num,pos) JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.num ORDER BY x.pos),
    k.convalidated,k.condeferrable,k.condeferred,k.confmatchtype,k.confupdtype,k.confdeltype,p.relkind,p.relpersistence
    FROM pg_constraint k JOIN pg_class p ON p.oid=k.confrelid JOIN pg_namespace n ON n.oid=p.relnamespace WHERE k.conrelid=to_regclass(:t) AND k.contype='f'"""),{'t':'public.'+table}).all()
    oid=c.execute(text("SELECT 'public.cost_identities'::regclass::oid")).scalar_one()
    wanted=(oid,'public','cost_identities',['order_uid','resource_kind','owner_scope','owner_key'],['identity_uid','resource_kind','owner_scope','owner_key'],True,False,False,'s','r','r','r','p')
    if rows != [wanted]: raise MigrationError('Global provenance public FK enforcement differs')

def _pg_shape(c, table):
    c.exec_driver_sql(ddl(table,True,reference=True))
    try:
        i=inspect(c);schema=c.execute(text('SELECT pg_my_temp_schema()::regnamespace::text')).scalar_one()
        def cols(name,schema=None): return [(x['name'],str(x['type']),x['nullable'],normal(x.get('default')),x.get('identity'),x.get('computed')) for x in i.get_columns(name,schema=schema)]
        if cols(table,'public') != cols('global_origin_reference',schema): raise MigrationError('Global provenance column shape differs')
        if i.get_pk_constraint(table)['constrained_columns'] != ['order_uid']: raise MigrationError('Global provenance primary key differs')
        unique=[tuple(x['column_names']) for x in i.get_unique_constraints(table)]
        if unique != ([('origin_nonce',)] if table==ORIGINS else []): raise MigrationError('Global provenance unique keys differ')
        if [x for x in i.get_indexes(table) if not x.get('duplicates_constraint')]: raise MigrationError('Global provenance extra index differs')
        def checks(name): return c.execute(text("SELECT conname,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(:n) AND contype='c' ORDER BY conname"),{'n':name}).all()
        if checks('public.'+table) != checks('pg_temp.global_origin_reference'): raise MigrationError('Global provenance CHECK differs')
        _pg_foreign(c,table)
        states=c.execute(text("SELECT convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(:t) AND contype IN ('p','u')"),{'t':'public.'+table}).all()
        if any(tuple(r)!=(True,False,False) for r in states): raise MigrationError('Global provenance key enforcement differs')
        indexes=c.execute(text('SELECT indisvalid,indisready,indimmediate,indnullsnotdistinct,indpred IS NULL,indexprs IS NULL,indnkeyatts=indnatts FROM pg_index WHERE indrelid=to_regclass(:t)'),{'t':'public.'+table}).all()
        if any(tuple(r)!=(True,True,True,False,True,True,True) for r in indexes): raise MigrationError('Global provenance key index differs')
    finally: c.exec_driver_sql('DROP TABLE global_origin_reference')

def validate(c):
    boundary(c)
    importlib.import_module('core.schema.migrations.009_cost_foundation').validate(c)
    importlib.import_module('core.schema.migrations.021_economic_identity').validate(c)
    i=inspect(c);pg=c.dialect.name=='postgresql'
    column={x['name']:x for x in i.get_columns('orders')}.get(SOURCE)
    if not column or str(column['type']).upper()!='TEXT' or not column['nullable'] or column.get('default') is not None or column.get('identity') or column.get('computed'): raise MigrationError('Global source column differs')
    source_keys(c,installed=True)
    if pg:
        for table in TABLES: _pg_shape(c,table)
        for name,body in pg_functions().items():
            rows=c.execute(text("SELECT p.prosrc,p.prosecdef,p.proconfig,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.pronargs,p.pronargdefaults,p.procost,p.prorows,p.prosupport::text,pg_get_function_result(p.oid),l.lanname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname=:n"),{'n':name}).all()
            if rows != [(body,False,['search_path=pg_catalog, public'],'v','u',False,False,'f',False,0,0,100.0,0.0,'-','trigger','plpgsql')]: raise MigrationError('Global provenance function differs')
        for (table,name),sql in pg_triggers().items():
            rows=c.execute(text("SELECT pg_get_triggerdef(oid),tgenabled,tgnargs,tgqual IS NULL,tgattr::text,tgconstraint,tgdeferrable,tginitdeferred,encode(tgargs,'hex') FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND tgname=:n"),{'t':'public.'+table,'n':name}).all()
            if rows != [(sql,'O',0,True,'',0,False,False,'')]: raise MigrationError('Global provenance trigger differs')
        for table in TABLES:
            names=set(c.execute(text('SELECT tgname FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND NOT tgisinternal'),{'t':'public.'+table}).scalars())
            if names != {n for (t,n) in pg_triggers() if t==table}: raise MigrationError('Global provenance extra trigger differs')
        rows=c.execute(text('SELECT indisvalid,indisready,indimmediate,indnullsnotdistinct,indpred IS NULL,indexprs IS NULL,indnkeyatts=indnatts FROM pg_index WHERE indexrelid=to_regclass(:n)'),{'n':'public.'+INDEX}).all()
        if rows != [(True,True,True,False,True,True,True)]: raise MigrationError('Global source unique enforcement differs')
        definition=c.execute(text('SELECT pg_get_indexdef(to_regclass(:n))'),{'n':'public.'+INDEX}).scalar_one()
        if definition!=f'CREATE UNIQUE INDEX {INDEX} ON public.orders USING btree ({SOURCE})': raise MigrationError('Global source unique definition differs')
    else:
        for table in TABLES:
            sql=c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),{'n':table}).scalar_one_or_none()
            if normal(sql)!=normal(ddl(table,False)): raise MigrationError('Global provenance table differs')
            if c.exec_driver_sql('PRAGMA foreign_key_check('+table+')').first(): raise MigrationError('Global provenance foreign data differs')
        expected=sqlite_triggers()
        got=dict(c.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (tbl_name IN ('cost_global_order_origins','cost_global_origin_exclusions') OR name LIKE 'orders_global_origin_%')")).all())
        if {k:normal(v) for k,v in got.items()} != {k:normal(v) for k,v in expected.items()}: raise MigrationError('Global provenance trigger differs')
        sql=c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'")).scalar_one()
        declarations=importlib.import_module('core.schema.migrations.021_economic_identity')._sqlite_declarations(sql)
        if [normal(p) for p in declarations if re.match(r'^economic_global_origin_nonce\s',p)] != [normal(SOURCE+' TEXT')]: raise MigrationError('Global source declaration differs')
        index=c.execute(text("SELECT sql FROM sqlite_master WHERE name=:n"),{'n':INDEX}).scalar_one()
        if normal(index)!=normal(f'CREATE UNIQUE INDEX {INDEX} ON orders({SOURCE})'): raise MigrationError('Global source index differs')
    # Check retained data independently of guards, including deleted source history.
    for table in TABLES:
        if c.execute(text(f"SELECT 1 FROM {table} e WHERE e.resource_kind IS DISTINCT FROM 'order' OR e.owner_scope IS DISTINCT FROM 'global' OR e.owner_key IS DISTINCT FROM 0 OR NOT ({registry('e')}) LIMIT 1")).first(): raise MigrationError('Global provenance retained registry differs')
    if c.execute(text(f'SELECT 1 FROM {ORIGINS} o JOIN {EXCLUSIONS} e USING(order_uid) LIMIT 1')).first(): raise MigrationError('Global provenance ledgers overlap')
    for nonce,proof,recorded in c.execute(text(f'SELECT origin_nonce,proof_kind,recorded_at FROM {ORIGINS}')):
        from datetime import datetime
        if proof!='verified_platform_admin_global_creation': raise MigrationError('Global provenance retained proof differs')
        try:
            if pg:
                if not isinstance(recorded,datetime): raise ValueError()
            elif datetime.strptime(recorded,'%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')!=recorded: raise ValueError()
        except (ValueError,TypeError): raise MigrationError('Global provenance retained time differs') from None
        if not isinstance(nonce,str) or not re.fullmatch('[0-9a-f]{32}',nonce): raise MigrationError('Global provenance retained nonce differs')
    if c.execute(text(f"SELECT 1 FROM orders s LEFT JOIN {ORIGINS} o ON o.order_uid=s.economic_uid WHERE s.{SOURCE} IS NOT NULL AND (s.org_id IS NOT NULL OR s.economic_uid IS NULL OR o.order_uid IS NULL OR o.origin_nonce IS DISTINCT FROM s.{SOURCE}) LIMIT 1")).first(): raise MigrationError('Global provenance live source lacks exact origin')
    if c.execute(text(f"SELECT 1 FROM {ORIGINS} o JOIN orders s ON s.economic_uid=o.order_uid WHERE s.org_id IS NOT NULL OR s.{SOURCE} IS DISTINCT FROM o.origin_nonce LIMIT 1")).first(): raise MigrationError('Global provenance origin differs from live source')

def apply(c):
    boundary(c)
    i=inspect(c);pg=c.dialect.name=='postgresql'
    footprint=SOURCE in {x['name'] for x in i.get_columns('orders')} or any(i.has_table(t) for t in TABLES)
    if pg:
        footprint=footprint or bool(c.execute(text("SELECT 1 FROM pg_proc WHERE proname IN ('orders_global_origin_guard','cost_global_order_origins_guard','cost_global_origin_exclusions_guard')")).first())
    else:
        footprint=footprint or bool(c.execute(text("SELECT 1 FROM sqlite_master WHERE name LIKE 'orders_global_origin_%' OR name LIKE 'cost_global_origin%' OR name LIKE 'cost_global_order_origin%'")).first())
    if footprint:
        validate(c)
        if c.execute(text(f'SELECT 1 FROM {ORIGINS} LIMIT 1')).first() or c.execute(text(f'SELECT 1 FROM orders WHERE {SOURCE} IS NOT NULL LIMIT 1')).first(): raise MigrationError('Global provenance unrecorded adoption is not unused')
        missing=c.execute(text(f"SELECT identity_uid FROM cost_identities WHERE resource_kind='order' AND owner_scope='global' AND owner_key=0 EXCEPT SELECT order_uid FROM {EXCLUSIONS}")).first()
        if missing: raise MigrationError('Global provenance cutover exclusions differ')
        return
    importlib.import_module('core.schema.migrations.009_cost_foundation').validate(c)
    importlib.import_module('core.schema.migrations.021_economic_identity').validate(c)
    source_keys(c,installed=False)
    for table in TABLES: c.exec_driver_sql(ddl(table,pg))
    c.exec_driver_sql('ALTER TABLE orders ADD COLUMN '+SOURCE+' TEXT')
    c.exec_driver_sql(f'CREATE UNIQUE INDEX {INDEX} ON orders({SOURCE})')
    if pg:
        for name,body in pg_functions().items(): c.exec_driver_sql('CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog, public AS $$'+body+'$$')
        for sql in pg_triggers().values(): c.exec_driver_sql(sql)
    else:
        for sql in sqlite_triggers().values(): c.exec_driver_sql(sql)
    c.exec_driver_sql(f"INSERT INTO {EXCLUSIONS}(order_uid,resource_kind,owner_scope,owner_key) SELECT identity_uid,resource_kind,owner_scope,owner_key FROM cost_identities WHERE resource_kind='order' AND owner_scope='global' AND owner_key=0")
    validate(c)
