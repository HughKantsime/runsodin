"""Additive prospective purchase evidence, outside frozen ORM metadata."""
import importlib
import re
from sqlalchemy import inspect,text
from core.schema.migrator import MigrationError
from core.spool_purchase_validation import canonical_decimal,MAX_REVISION
from core.finance_currency import SUPPORTED_CURRENCIES
TABLE='spool_purchase_evidence'
COLUMNS=('purchase_evidence_nonce','purchase_revision')

def _normal(sql):
    return ''.join(x if x.startswith("'") else re.sub(r'\s+','',x).lower() for x in re.findall(r"'(?:''|[^'])*'|[^']+",str(sql))).rstrip(';')

def number_check(column,pg,positive=False):
    if pg:rule=f"{column} ~ '^(0|[1-9][0-9]{{0,17}})(\\.[0-9]{{0,11}}[1-9])?$'"
    else:
        rule=f"typeof({column})='text' AND length({column}) BETWEEN 1 AND 31 AND {column} NOT GLOB '*[^0-9.]*' AND ((instr({column},'.')=0 AND length({column})<=18 AND ({column}='0' OR substr({column},1,1) BETWEEN '1' AND '9')) OR (instr({column},'.') BETWEEN 2 AND 19 AND length(substr({column},instr({column},'.')+1)) BETWEEN 1 AND 12 AND substr({column},instr({column},'.')+1) NOT GLOB '*.*' AND substr({column},-1) BETWEEN '1' AND '9' AND (substr({column},1,1) BETWEEN '1' AND '9' OR substr({column},1,instr({column},'.')-1)='0')))"
    return rule+(f" AND {column}<>'0'" if positive else '')

def ddl(pg,name=TABLE,temporary=False):
    nonce="evidence_nonce ~ '^[0-9a-f]{32}$'" if pg else "typeof(evidence_nonce)='text' AND length(evidence_nonce)=32 AND evidence_nonce NOT GLOB '*[^0-9a-f]*'"
    inttype='' if pg else " AND typeof(revision)='integer' AND typeof(owner_key)='integer'"
    timestamp="TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('UTC',CURRENT_TIMESTAMP)" if pg else 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'
    timecheck="isfinite(recorded_at)" if pg else "typeof(recorded_at)='text' AND length(recorded_at)=19 AND strftime('%Y-%m-%d %H:%M:%S',recorded_at) IS NOT NULL AND strftime('%Y-%m-%d %H:%M:%S',recorded_at)=recorded_at"
    result=f"""CREATE {'TEMPORARY ' if temporary else ''}TABLE {name}(
 evidence_nonce TEXT PRIMARY KEY NOT NULL,
 spool_uid TEXT NOT NULL,birth_nonce TEXT NOT NULL,resource_kind TEXT NOT NULL DEFAULT 'spool',
 owner_scope TEXT NOT NULL,owner_key INTEGER NOT NULL,revision INTEGER NOT NULL,
 currency TEXT NOT NULL,price_decimal TEXT NOT NULL,net_weight_g_decimal TEXT NOT NULL,
 weight_basis TEXT NOT NULL DEFAULT 'purchased_net_filament_g',provenance TEXT NOT NULL DEFAULT 'owner_attestation',recorded_at {timestamp},
 CONSTRAINT spe_nonce CHECK({nonce}),
 CONSTRAINT spe_owner CHECK(resource_kind='spool' AND ((owner_scope='global' AND owner_key=0) OR (owner_scope='org' AND owner_key>0)) AND revision BETWEEN 1 AND {MAX_REVISION}{inttype}),
 CONSTRAINT spe_currency CHECK(currency IN ({','.join(repr(c) for c in SUPPORTED_CURRENCIES)})),
 CONSTRAINT spe_price CHECK({number_check('price_decimal',pg)}),
 CONSTRAINT spe_weight CHECK({number_check('net_weight_g_decimal',pg,True)}),
 CONSTRAINT spe_basis CHECK(weight_basis='purchased_net_filament_g' AND provenance='owner_attestation'),
 CONSTRAINT spe_time CHECK({timecheck}),
 CONSTRAINT spe_revision UNIQUE(birth_nonce,revision),
 CONSTRAINT spe_birth FOREIGN KEY(birth_nonce) REFERENCES cost_source_lifetimes(birth_nonce) ON DELETE RESTRICT,
 CONSTRAINT spe_identity FOREIGN KEY(spool_uid,resource_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT
)"""
    if temporary:result=result[:result.index(",\n CONSTRAINT spe_birth")]+"\n)"
    return result

def _evidence_proof(n='NEW'):
    return f"EXISTS(SELECT 1 FROM spools s JOIN cost_source_lifetimes l ON l.birth_nonce=s.economic_birth_nonce WHERE s.economic_uid={n}.spool_uid AND s.economic_birth_nonce={n}.birth_nonce AND l.resource_kind='spool' AND l.resource_id=s.id AND l.state='known' AND l.identity_uid={n}.spool_uid AND l.owner_scope={n}.owner_scope AND l.owner_key={n}.owner_key AND {n}.owner_scope=CASE WHEN s.org_id IS NULL THEN 'global' ELSE 'org' END AND {n}.owner_key=COALESCE(s.org_id,0) AND {n}.revision=COALESCE(s.purchase_revision,0)+1)"

def _pointer_proof(n='NEW'):
    return f"EXISTS(SELECT 1 FROM {TABLE} e JOIN cost_source_lifetimes l ON l.birth_nonce=e.birth_nonce WHERE e.evidence_nonce={n}.purchase_evidence_nonce AND e.revision={n}.purchase_revision AND e.spool_uid={n}.economic_uid AND e.birth_nonce={n}.economic_birth_nonce AND l.resource_kind='spool' AND l.resource_id={n}.id AND l.identity_uid=e.spool_uid AND l.state='known' AND l.owner_scope=e.owner_scope AND l.owner_key=e.owner_key AND e.owner_scope=CASE WHEN {n}.org_id IS NULL THEN 'global' ELSE 'org' END AND e.owner_key=COALESCE({n}.org_id,0))"

def sqlite_triggers():
    unchanged='NEW.purchase_evidence_nonce IS OLD.purchase_evidence_nonce AND NEW.purchase_revision IS OLD.purchase_revision'
    nextstep=f"OLD.purchase_revision IS NOT {MAX_REVISION} AND NEW.purchase_revision IS NOT NULL AND NEW.purchase_revision=COALESCE(OLD.purchase_revision,0)+1 AND (NEW.purchase_evidence_nonce IS NULL OR (NEW.purchase_evidence_nonce IS NOT OLD.purchase_evidence_nonce AND {_pointer_proof()}))"
    financial='NEW.price IS NOT OLD.price OR NEW.initial_weight_g IS NOT OLD.initial_weight_g'
    return {
      'spool_purchase_evidence_insert':f"CREATE TRIGGER spool_purchase_evidence_insert BEFORE INSERT ON {TABLE} BEGIN SELECT CASE WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE evidence_nonce=NEW.evidence_nonce OR (birth_nonce=NEW.birth_nonce AND revision=NEW.revision)) OR NOT ({_evidence_proof()}) THEN RAISE(ABORT,'purchase evidence source differs') END; END",
      'spool_purchase_evidence_no_update':f"CREATE TRIGGER spool_purchase_evidence_no_update BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT,'purchase evidence is immutable'); END",
      'spool_purchase_evidence_no_delete':f"CREATE TRIGGER spool_purchase_evidence_no_delete BEFORE DELETE ON {TABLE} BEGIN SELECT RAISE(ABORT,'purchase evidence is immutable'); END",
      'spools_purchase_insert':"CREATE TRIGGER spools_purchase_insert BEFORE INSERT ON spools BEGIN SELECT CASE WHEN NEW.purchase_evidence_nonce IS NOT NULL OR NEW.purchase_revision IS NOT NULL THEN RAISE(ABORT,'fresh purchase state required') END; END",
      'spools_purchase_transition':f"CREATE TRIGGER spools_purchase_transition BEFORE UPDATE ON spools BEGIN SELECT CASE WHEN NOT (({unchanged}) OR ({nextstep})) OR (NEW.purchase_evidence_nonce IS NOT NULL AND NOT ({_pointer_proof()})) THEN RAISE(ABORT,'purchase transition differs') END; END",
      'spools_purchase_invalidate':f"CREATE TRIGGER spools_purchase_invalidate AFTER UPDATE ON spools WHEN OLD.purchase_evidence_nonce IS NOT NULL AND ({financial}) AND ({unchanged}) BEGIN SELECT CASE WHEN OLD.purchase_revision={MAX_REVISION} THEN RAISE(ABORT,'purchase revision exhausted') END; UPDATE spools SET purchase_evidence_nonce=NULL,purchase_revision=OLD.purchase_revision+1 WHERE id=NEW.id; END",
    }

def pg_functions():
    unchanged='NEW.purchase_evidence_nonce IS NOT DISTINCT FROM OLD.purchase_evidence_nonce AND NEW.purchase_revision IS NOT DISTINCT FROM OLD.purchase_revision'
    nextstep=f"OLD.purchase_revision IS DISTINCT FROM {MAX_REVISION} AND NEW.purchase_revision IS NOT NULL AND NEW.purchase_revision=COALESCE(OLD.purchase_revision,0)+1 AND (NEW.purchase_evidence_nonce IS NULL OR (NEW.purchase_evidence_nonce IS DISTINCT FROM OLD.purchase_evidence_nonce AND {_pointer_proof()}))"
    financial='NEW.price IS DISTINCT FROM OLD.price OR NEW.initial_weight_g IS DISTINCT FROM OLD.initial_weight_g'
    return {
      'spool_purchase_evidence_guard':f"BEGIN IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'purchase evidence is immutable' USING ERRCODE='23514'; END IF; IF NOT ({_evidence_proof()}) THEN RAISE EXCEPTION 'purchase evidence source differs' USING ERRCODE='23514'; END IF; RETURN NEW; END",
      'spools_purchase_guard':f"BEGIN IF TG_OP='INSERT' THEN IF NEW.purchase_evidence_nonce IS NOT NULL OR NEW.purchase_revision IS NOT NULL THEN RAISE EXCEPTION 'fresh purchase state required' USING ERRCODE='23514'; END IF; RETURN NEW; END IF; IF OLD.purchase_evidence_nonce IS NOT NULL AND ({financial}) AND ({unchanged}) THEN IF OLD.purchase_revision={MAX_REVISION} THEN RAISE EXCEPTION 'purchase revision exhausted' USING ERRCODE='23514'; END IF; NEW.purchase_evidence_nonce=NULL; NEW.purchase_revision=OLD.purchase_revision+1; END IF; IF NOT (({unchanged}) OR ({nextstep})) OR (NEW.purchase_evidence_nonce IS NOT NULL AND NOT ({_pointer_proof()})) THEN RAISE EXCEPTION 'purchase transition differs' USING ERRCODE='23514'; END IF; RETURN NEW; END",
    }

def pg_triggers():return {
 ('spools','spools_purchase_guard'):'CREATE TRIGGER spools_purchase_guard BEFORE INSERT OR UPDATE ON public.spools FOR EACH ROW EXECUTE FUNCTION spools_purchase_guard()',
 (TABLE,'spool_purchase_evidence_guard'):f'CREATE TRIGGER spool_purchase_evidence_guard BEFORE INSERT OR DELETE OR UPDATE ON public.{TABLE} FOR EACH ROW EXECUTE FUNCTION spool_purchase_evidence_guard()',
 (TABLE,'spool_purchase_evidence_no_truncate'):f'CREATE TRIGGER spool_purchase_evidence_no_truncate BEFORE TRUNCATE ON public.{TABLE} FOR EACH STATEMENT EXECUTE FUNCTION spool_purchase_evidence_guard()',
}


def _boundary(c):
    if c.dialect.name=='postgresql':
        if c.execute(text('SELECT current_schema()')).scalar_one()!='public':raise MigrationError('Purchase schema differs')
        for name in ('spools',TABLE):
            oid,qualified=c.execute(text('SELECT to_regclass(:n)::oid,to_regclass(:q)::oid'),{'n':name,'q':'public.'+name}).one()
            if oid!=qualified:raise MigrationError('Purchase relation shadowed')
            if oid is not None:
                if c.execute(text('SELECT relkind,relpersistence FROM pg_class WHERE oid=:oid'),{'oid':oid}).one()!=('r','p'):raise MigrationError('Purchase relation differs')
                c.exec_driver_sql('LOCK TABLE public.'+name+' IN SHARE MODE NOWAIT')
    elif c.dialect.name=='sqlite':
        driver=c.connection.driver_connection
        if driver.execute('PRAGMA foreign_keys').fetchone()[0]!=1:raise MigrationError('Purchase foreign keys required')
        if not driver.in_transaction:raise MigrationError('Purchase writer transaction required')
        if c.execute(text("SELECT 1 FROM sqlite_temp_master WHERE name IN ('spools','spool_purchase_evidence')")).first():raise MigrationError('Purchase relation shadowed')
        c.execute(text('UPDATE spools SET id=id WHERE 0'))
    else:raise MigrationError('Unsupported purchase dialect')


def _contract(c,table):
    i=inspect(c)
    columns=[(x['name'],str(x['type'].compile(dialect=c.dialect)),x['nullable'],_normal(x.get('default'))) for x in i.get_columns(table)]
    fk=[]
    for f in i.get_foreign_keys(table):fk.append((tuple(f['constrained_columns']),f['referred_table'],tuple(f['referred_columns']),f.get('options',{}).get('ondelete')))
    return columns,i.get_pk_constraint(table)['constrained_columns'],sorted(tuple(x['column_names']) for x in i.get_unique_constraints(table)),sorted(fk),[x for x in i.get_indexes(table) if not x.get('duplicates_constraint')]


def _pg_checks(c,name):
    rows=c.execute(text("SELECT conname,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(:n) AND contype='c'"),{'n':name}).all()
    if any(not r[2] or r[3] or r[4] for r in rows):raise MigrationError('Purchase CHECK enforcement differs')
    return {r[0]:r[1] for r in rows}


def _pg_foreign_keys(c,table,expected,*,selected_column=None):
    # Compare catalog OIDs and ordered attribute identities, never regclass display
    # strings (which vary with search_path and can hide same-name foreign targets).
    rows=c.execute(text("""SELECT
        ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY x(attnum,position)
              JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.attnum ORDER BY x.position),
        k.confrelid,n.nspname,p.relname,p.relkind,p.relpersistence,
        ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY x(attnum,position)
              JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.attnum ORDER BY x.position),
        k.convalidated,k.condeferrable,k.condeferred,k.confmatchtype,k.confupdtype,k.confdeltype
        FROM pg_constraint k JOIN pg_class p ON p.oid=k.confrelid JOIN pg_namespace n ON n.oid=p.relnamespace
        WHERE k.conrelid=to_regclass(:table) AND k.contype='f'
        AND (CAST(:column AS TEXT) IS NULL OR (SELECT attnum FROM pg_attribute WHERE attrelid=k.conrelid AND attname=:column)=ANY(k.conkey))"""),
        {'table':'public.'+table,'column':selected_column}).all()
    wanted=[]
    for columns,(target,target_columns) in expected.items():
        oid=c.execute(text('SELECT to_regclass(:target)::oid'),{'target':'public.'+target}).scalar_one()
        if oid is None:raise MigrationError('Purchase public FK target missing')
        wanted.append((columns,oid,'public',target,'r','p',target_columns,True,False,False,'s','a','r'))
    actual=[(tuple(r[0]),*r[1:6],tuple(r[6]),*r[7:]) for r in rows]
    if sorted(actual)!=sorted(wanted):raise MigrationError('Purchase public FK enforcement differs')


def validate(c):
    _boundary(c)
    previous=importlib.import_module('core.schema.migrations.022_economic_source_lifetimes');previous.validate(c)
    pg=c.dialect.name=='postgresql'
    i=inspect(c);cols={x['name']:x for x in i.get_columns('spools')}
    for name,kind in zip(COLUMNS,('TEXT','INTEGER')):
        x=cols.get(name)
        if not x or str(x['type']).upper()!=kind or not x['nullable'] or x.get('default') is not None:raise MigrationError('Purchase source column differs')
    fs=[f for f in i.get_foreign_keys('spools') if 'purchase_evidence_nonce' in f['constrained_columns']]
    if len(fs)!=1 or fs[0]['constrained_columns']!=['purchase_evidence_nonce'] or fs[0]['referred_table']!=TABLE or fs[0]['referred_columns']!=['evidence_nonce'] or (pg and fs[0].get('options',{}).get('ondelete')!='RESTRICT'):raise MigrationError('Purchase pointer FK differs')
    if not i.has_table(TABLE):raise MigrationError('Purchase table missing')
    if pg:
        c.exec_driver_sql(ddl(True,'purchase_contract_reference',True))
        try:
            schema=c.execute(text('SELECT pg_my_temp_schema()::regnamespace::text')).scalar_one()
            actual=_contract(c,TABLE)
            ref_i=inspect(c)
            refcols=[(x['name'],str(x['type'].compile(dialect=c.dialect)),x['nullable'],_normal(x.get('default'))) for x in ref_i.get_columns('purchase_contract_reference',schema=schema)]
            if actual[0]!=refcols or actual[1]!=['evidence_nonce'] or actual[2]!=[('birth_nonce','revision')] or len(actual[3])!=2 or actual[4]:raise MigrationError('Purchase table/key shape differs')
            if _pg_checks(c,TABLE)!=_pg_checks(c,'pg_temp.purchase_contract_reference'):raise MigrationError('Purchase CHECK differs')
        finally:c.exec_driver_sql('DROP TABLE purchase_contract_reference')
        expected_fk={('birth_nonce',):('cost_source_lifetimes',('birth_nonce',)),('spool_uid','resource_kind','owner_scope','owner_key'):('cost_identities',('identity_uid','resource_kind','owner_scope','owner_key'))}
        if {tuple(f['constrained_columns']):(f['referred_table'],tuple(f['referred_columns'])) for f in i.get_foreign_keys(TABLE)}!=expected_fk:raise MigrationError('Purchase typed FK differs')
        _pg_foreign_keys(c,TABLE,expected_fk)
        _pg_foreign_keys(c,'spools',{('purchase_evidence_nonce',):(TABLE,('evidence_nonce',))},selected_column='purchase_evidence_nonce')
        for name,body in pg_functions().items():
            rows=c.execute(text("SELECT p.prosrc,p.prosecdef,p.proconfig,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.pronargs,p.pronargdefaults,p.procost,p.prorows,p.prosupport::text,pg_get_function_result(p.oid),l.lanname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname=:n"),{'n':name}).all()
            if rows!=[(body,False,['search_path=pg_catalog, public'],'v','u',False,False,'f',False,0,0,100.0,0.0,'-','trigger','plpgsql')]:raise MigrationError('Purchase function differs')
        for (table,name),sql in pg_triggers().items():
            rows=c.execute(text("SELECT pg_get_triggerdef(oid),tgenabled,tgnargs,tgqual IS NULL,tgattr::text,tgconstraint,tgdeferrable,tginitdeferred,encode(tgargs,'hex') FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND tgname=:n"),{'t':'public.'+table,'n':name}).all()
            if rows!=[(sql,'O',0,True,'',0,False,False,'')]:raise MigrationError('Purchase trigger differs')
        extra=c.execute(text("SELECT tgname FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND NOT tgisinternal"),{'t':TABLE}).scalars().all()
        if set(extra)!={n for (t,n) in pg_triggers() if t==TABLE}:raise MigrationError('Purchase extra trigger differs')
        source_checks=_pg_checks(c,'spools')
        if source_checks.get('spools_purchase_revision_check')!='CHECK (((purchase_revision IS NULL) OR ((purchase_revision >= 1) AND (purchase_revision <= 2147483647))))':raise MigrationError('Purchase source revision CHECK differs')
    else:
        raw=c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),{'n':TABLE}).scalar_one()
        if _normal(raw)!=_normal(ddl(False)):raise MigrationError('Purchase table contract differs')
        got=dict(c.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (tbl_name=:n OR name LIKE 'spools_purchase_%')"),{'n':TABLE}).all())
        if {n:_normal(v) for n,v in got.items()}!={n:_normal(v) for n,v in sqlite_triggers().items()}:raise MigrationError('Purchase trigger differs')
        raw_source=c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='spools'")).scalar_one()
        for declaration in ("purchase_evidence_nonce TEXT REFERENCES spool_purchase_evidence(evidence_nonce) ON DELETE RESTRICT", "purchase_revision INTEGER CONSTRAINT spools_purchase_revision_check CHECK(purchase_revision IS NULL OR (typeof(purchase_revision)='integer' AND purchase_revision BETWEEN 1 AND 2147483647))"):
            if _normal(declaration) not in _normal(raw_source):raise MigrationError('Purchase source declaration differs')
    for r in c.execute(text(f'SELECT price_decimal,net_weight_g_decimal,recorded_at FROM {TABLE}')).all():  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
        if not canonical_decimal(r[0]) or not canonical_decimal(r[1],positive=True):raise MigrationError('Retained purchase decimal differs')
        from datetime import datetime
        try:
            dt=r[2] if isinstance(r[2],datetime) else datetime.strptime(r[2],'%Y-%m-%d %H:%M:%S')
            if not pg and dt.strftime('%Y-%m-%d %H:%M:%S')!=r[2]:raise ValueError()
        except (ValueError,TypeError):raise MigrationError('Retained purchase timestamp differs') from None
    bad=c.execute(text(f"SELECT 1 FROM {TABLE} e LEFT JOIN cost_source_lifetimes l ON l.birth_nonce=e.birth_nonce WHERE l.state IS DISTINCT FROM 'known' OR l.resource_kind IS DISTINCT FROM 'spool' OR l.identity_uid IS DISTINCT FROM e.spool_uid OR l.owner_scope IS DISTINCT FROM e.owner_scope OR l.owner_key IS DISTINCT FROM e.owner_key LIMIT 1")).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
    if bad:raise MigrationError('Retained purchase lifetime differs')
    if c.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
        f"""SELECT 1 FROM spools s JOIN
        (SELECT spool_uid,birth_nonce,MAX(revision) AS retained_revision FROM {TABLE} GROUP BY spool_uid,birth_nonce) e
        ON e.spool_uid=s.economic_uid AND e.birth_nonce=s.economic_birth_nonce
        WHERE s.purchase_revision IS NULL OR s.purchase_revision<e.retained_revision LIMIT 1""")).first():
        raise MigrationError('Retained purchase counter precedes history')
    if c.execute(text(f'SELECT 1 FROM spools s WHERE s.purchase_evidence_nonce IS NOT NULL AND NOT ({_pointer_proof("s")}) LIMIT 1')).first():raise MigrationError('Retained purchase pointer differs')  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input


def apply(c):
    _boundary(c)
    pg=c.dialect.name=='postgresql';i=inspect(c)
    if i.has_table(TABLE) or any(n in {x['name'] for x in i.get_columns('spools')} for n in COLUMNS):validate(c);return
    if pg:
        if c.execute(text("SELECT 1 FROM pg_proc WHERE proname IN ('spools_purchase_guard','spool_purchase_evidence_guard')")).first():raise MigrationError('Purchase partial function footprint')
    elif c.execute(text("SELECT 1 FROM sqlite_master WHERE type='trigger' AND (name LIKE 'spools_purchase_%' OR name LIKE 'spool_purchase_evidence_%')")).first():raise MigrationError('Purchase partial trigger footprint')
    importlib.import_module('core.schema.migrations.022_economic_source_lifetimes').validate(c)
    c.exec_driver_sql(ddl(pg))
    c.exec_driver_sql('ALTER TABLE spools ADD COLUMN purchase_evidence_nonce TEXT REFERENCES spool_purchase_evidence(evidence_nonce) ON DELETE RESTRICT')
    revision='purchase_revision IS NULL OR (purchase_revision BETWEEN 1 AND 2147483647)' if pg else "purchase_revision IS NULL OR (typeof(purchase_revision)='integer' AND purchase_revision BETWEEN 1 AND 2147483647)"
    c.exec_driver_sql('ALTER TABLE spools ADD COLUMN purchase_revision INTEGER CONSTRAINT spools_purchase_revision_check CHECK('+revision+')')
    if pg:
        for name,body in pg_functions().items():c.exec_driver_sql('CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog, public AS $$'+body+'$$')
        for sql in pg_triggers().values():c.exec_driver_sql(sql)
    else:
        for sql in sqlite_triggers().values():c.exec_driver_sql(sql)
    validate(c)
