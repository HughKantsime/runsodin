"""Independent bounded arithmetic/hash/retained-link validation; no commits/auth."""
import hashlib,json,re
from datetime import datetime,timezone
from fractions import Fraction
from sqlalchemy import inspect,text
from core import priced_estimate_schema as s
from core.priced_job_estimate import calculate_priced_job_estimate,EstimateInputError,_decimal


def canonical_json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def digest(value):return hashlib.sha256(canonical_json(value).encode()).hexdigest()
def _fail():s.refuse()
def _json(raw):
    if type(raw) is not str or len(raw.encode())>65536:_fail()
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:_fail()
            result[key]=value
        return result
    try:value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _: _fail())
    except (ValueError,TypeError):_fail()
    if canonical_json(value)!=raw:_fail()
    return value


def selection_command(row):
    return dict(purpose='priced_selection',owner_scope=row['owner_scope'],owner_key=row['owner_key'],actor_binding_uid=row['actor_binding_uid'],expected_revision=row['expected_revision'],source={n:row[n] for n in s.SOURCE_LINK})

def reference_command(row):
    return dict(purpose='job_estimate_reference',job_uid=row['job_uid'],owner_scope=row['owner_scope'],owner_key=row['owner_key'],actor_binding_uid=row['actor_binding_uid'],expected_revision=row['expected_revision'],receipt_uid=row['receipt_uid'],invalidation_reason=row['invalidation_reason'],current_input_sha256=row['current_input_sha256'])


def _execute(db,sql,params=None):
    if db.dialect.name=='postgresql':
        names=(*s.TABLES,'cost_identities')
        sql=re.sub(r'(?<=FROM )('+ '|'.join(names)+r')\b',r'public.\1',sql)
    return db.execute(text(sql),params or {})

def _rows(db,sql,params=None):return _execute(db,sql,params).mappings()
def _one(db,sql,params):
    rows=_rows(db,sql,params).fetchmany(2)
    if len(rows)!=1:_fail()
    return rows[0]

def _group(db,table,where,params,revision='revision'):
    rows=_rows(db,'SELECT * FROM '+table+' WHERE '+where+' ORDER BY '+revision+' LIMIT 4097',params).all()
    if len(rows)>4096 or [r[revision] for r in rows]!=list(range(1,len(rows)+1)):_fail()
    return rows


def _time(value):
    if isinstance(value,datetime):
        if value.tzinfo is None or value.year<1:_fail()
        return
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z',value):_fail()
    try:datetime.strptime(value,'%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError:_fail()


def validate_source(row):
    bundle=_json(row['assumptions_json'])
    if type(bundle) is not dict or set(bundle)!= {'currency','assumptions','material_rates'} or bundle['currency']!=row['currency']:_fail()
    if type(bundle['assumptions']) is not dict or set(bundle['assumptions'])!=set(s.ACTIVE_FIELDS):_fail()
    prepared={**bundle,'assumptions':{**bundle['assumptions'],'spool_cost':'0','spool_weight':'1'}}
    try:calculate_priced_job_estimate(prepared,material_key=None,runs=1)
    except EstimateInputError:_fail()
    if digest(bundle)!=row['source_sha256']:_fail()
    _time(row['created_at'])
    return prepared


INPUT_FIELDS=('material_key','grams_per_run','hours_per_run','runs','payload_sha256','plate_index','piece_count','capacity','model_fingerprint','file_fingerprint')

def validate_receipt(db,row):
    identity=_one(db,'SELECT resource_kind,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid',{'uid':row['job_uid']})
    if (identity['resource_kind'],identity['owner_scope'],identity['owner_key'])!=('job',row['owner_scope'],row['owner_key']):_fail()
    _time(row['created_at']);inputs=_json(row['input_json'])
    if type(inputs) is not dict or set(inputs)!=set(INPUT_FIELDS) or digest(inputs)!=row['input_sha256']:_fail()
    if inputs['material_key'] is not None and (type(inputs['material_key']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',inputs['material_key'])):_fail()
    if inputs['runs'] is not None and (type(inputs['runs']) is not int or not 1<=inputs['runs']<=10000):_fail()
    for field in ('grams_per_run','hours_per_run'):
        if inputs[field] is not None:
            try:_decimal(inputs[field],maximum=10**9)
            except EstimateInputError:_fail()
    for field in ('plate_index','piece_count','capacity'):
        if inputs[field] is not None and (type(inputs[field]) is not int or inputs[field]<(0 if field=='plate_index' else 1)):_fail()
    for field in ('payload_sha256','plate_index','piece_count','capacity'):
        if inputs[field]!=row[field]:_fail()
    for field in ('payload_sha256','model_fingerprint','file_fingerprint'):
        if inputs[field] is not None and (type(inputs[field]) is not str or not re.fullmatch('[0-9a-f]{64}',inputs[field])):_fail()
    components=_rows(db,'SELECT * FROM '+s.COMPONENT+' WHERE receipt_uid=:uid ORDER BY component_name LIMIT 12',{'uid':row['receipt_uid']}).all()
    if row['classification']=='unknown':
        if components or any(row[n] is not None for n in (*s.SOURCE_LINK,'amount','runs','required_total_name','selection_owner_scope','selection_owner_key','selection_revision')):_fail()
        reason=row['unknown_reason']
        if reason not in s.REASONS:_fail()
        if reason=='missing_material' and inputs['material_key'] is not None:_fail()
        if reason=='missing_geometry' and inputs['grams_per_run'] is not None and inputs['hours_per_run'] is not None:_fail()
        if reason=='missing_run_count' and inputs['runs'] is not None:_fail()
        return
    if row['classification'] not in ('job_estimate','scenario') or row['required_total_name']!='total' or row['formula']!='per-run-cost-v1' or row['rounding']!='HALF_EVEN' or row['unknown_reason'] is not None:_fail()
    h=_one(db,'SELECT * FROM '+s.SELECTION_HISTORY+' WHERE owner_scope=:scope AND owner_key=:key AND revision=:revision',{'scope':row['selection_owner_scope'],'key':row['selection_owner_key'],'revision':row['selection_revision']})
    if (h['owner_scope'],h['owner_key'])!=(row['owner_scope'],row['owner_key']) or tuple(h[n] for n in s.SOURCE_LINK)!=tuple(row[n] for n in s.SOURCE_LINK):_fail()
    source=_one(db,'SELECT * FROM '+s.SOURCE+' WHERE source_uid=:uid AND revision=:revision',{'uid':row['source_uid'],'revision':row['source_revision']})
    if (source['owner_scope'],source['owner_key'],source['currency'])!=(row['source_owner_scope'],row['source_owner_key'],row['currency']):_fail()
    prepared=validate_source(source)
    try:expected=calculate_priced_job_estimate(prepared,material_key=inputs['material_key'],grams_per_run=inputs['grams_per_run'],hours_per_run=inputs['hours_per_run'],runs=inputs['runs'])
    except EstimateInputError:_fail()
    if expected.classification!='job_estimate' or (expected.currency,expected.amount,expected.runs)!=(row['currency'],row['amount'],row['runs']):_fail()
    actual={r['component_name']:r for r in components}
    if set(actual)!=set(s.NAMES) or len(components)!=11:_fail()
    for component in expected.components:
        record=actual[component.name]
        if (record['currency'],record['basis'],record['predecessor_name'],record['numerator'],record['denominator'])!=(component.currency,component.basis,s.PREDECESSOR[component.name],str(component.numerator),str(component.denominator)):_fail()


def _local_checks(db):
    """Re-evaluate every declared local CHECK/NOTNULL from trusted definitions.

    SQL CHECK ordinarily admits NULL; the retained proof explicitly requires
    each expression TRUE, and retains declared nullable all-NULL exceptions.
    No row payload is fetched while checking scalar shape.
    """
    for table,(columns,rules) in s.definitions(db.dialect.name=='postgresql').items():
        predicates=[]
        for column in columns:
            name=column.split(' ',1)[0]
            if ' NOT NULL' in column:predicates.append(name+' IS NULL')
            if ' CHECK(' in column:
                expression=column.split(' CHECK(',1)[1][:-1]
                predicates.append('('+expression+') IS NOT TRUE')
        predicates.extend('('+rule[6:-1]+') IS NOT TRUE' for rule in rules if rule.startswith('CHECK('))
        if _execute(db,'SELECT 1 FROM '+table+' WHERE '+' OR '.join(predicates)+' LIMIT 1').first():_fail()


def validate_data(db):
    """Stream groups; fetch<=4097 history rows, preflight payload bytes first."""
    _local_checks(db)
    for table,fields in ((s.SOURCE,('assumptions_json',)),(s.RECEIPT,('input_json',))):
        length='octet_length' if db.dialect.name=='postgresql' else 'length(CAST({} AS BLOB))'
        for field in fields:
            expression=length+'('+field+')' if db.dialect.name=='postgresql' else length.format(field)
            if _execute(db,'SELECT 1 FROM '+table+' WHERE '+expression+'>65536 LIMIT 1').first():_fail()
    if _execute(db,'SELECT 1 FROM '+s.SOURCE+' AS NEW WHERE '+s.source_json_invalid(db.dialect.name=='postgresql')+' LIMIT 1').first():_fail()
    for table,key,revision in ((s.SOURCE,'source_uid','revision'),(s.RECEIPT,'job_uid','receipt_revision'),(s.REFERENCE_HISTORY,'job_uid','revision')):
        keys=_rows(db,'SELECT DISTINCT '+key+' FROM '+table)
        while chunk:=keys.fetchmany(128):
            for value in chunk:
                group=_group(db,table,key+'=:key',{'key':value[key]},revision)
                if table==s.SOURCE:
                    if len({(r['owner_scope'],r['owner_key']) for r in group})!=1:_fail()
                    for row in group:validate_source(row)
                elif table==s.RECEIPT:
                    for row in group:validate_receipt(db,row)
                else:
                    for row in group:_validate_reference(db,row)
                    current=_one(db,'SELECT * FROM '+s.CURRENT+' WHERE job_uid=:key',{'key':value[key]})
                    if current['revision']!=group[-1]['revision'] or any(current[n]!=group[-1][n] for n in ('owner_scope','owner_key','receipt_uid','invalidation_reason','current_input_sha256')):_fail()
    keys=_rows(db,'SELECT DISTINCT owner_scope,owner_key FROM '+s.SELECTION_HISTORY)
    while chunk:=keys.fetchmany(128):
        for key in chunk:
            group=_group(db,s.SELECTION_HISTORY,'owner_scope=:scope AND owner_key=:key',{'scope':key['owner_scope'],'key':key['owner_key']})
            for row in group:
                _time(row['created_at'])
                if row['expected_revision']!=row['revision']-1 or digest(selection_command(row))!=row['request_sha256']:_fail()
                if row['source_uid'] is not None:
                    source=_one(db,'SELECT * FROM '+s.SOURCE+' WHERE source_uid=:uid AND revision=:revision',{'uid':row['source_uid'],'revision':row['source_revision']})
                    if (source['owner_scope'],source['owner_key'],source['currency'])!=(row['source_owner_scope'],row['source_owner_key'],row['currency']):_fail()
                    if (source['owner_scope'],source['owner_key']) not in (('global',0),(row['owner_scope'],row['owner_key'])):_fail()
                elif any(row[n] is not None for n in s.SOURCE_LINK):_fail()
            current=_one(db,'SELECT * FROM '+s.SELECTION+' WHERE owner_scope=:scope AND owner_key=:key',{'scope':key['owner_scope'],'key':key['owner_key']})
            if current['revision']!=group[-1]['revision']:_fail()
    # Reject current rows with no history, orphan components and dangling links
    # independently, even when a restore caller had disabled FK enforcement.
    inspector=inspect(db)
    for table in s.TABLES:
        for fk in inspector.get_foreign_keys(table,schema='public' if db.dialect.name=='postgresql' else None):
            cols=fk['constrained_columns'];target=fk['referred_table'];targets=fk['referred_columns']
            sql='SELECT 1 FROM '+table+' a WHERE '+ ' AND '.join('a.'+c+' IS NOT NULL' for c in cols)+' AND NOT EXISTS(SELECT 1 FROM '+target+' b WHERE '+' AND '.join('a.'+a+'=b.'+b for a,b in zip(cols,targets))+') LIMIT 1'
            if _execute(db,sql).first():_fail()


def _validate_reference(db,row):
    _time(row['created_at'])
    identity=_one(db,'SELECT resource_kind,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid',{'uid':row['job_uid']})
    if tuple(identity[n] for n in ('resource_kind','owner_scope','owner_key'))!=('job',row['owner_scope'],row['owner_key']) or row['expected_revision']!=row['revision']-1 or digest(reference_command(row))!=row['request_sha256']:_fail()
    if row['receipt_uid'] is None:
        if row['invalidation_reason'] not in s.INVALIDATIONS or row['current_input_sha256'] is not None:_fail()
    else:
        receipt=_one(db,'SELECT * FROM '+s.RECEIPT+' WHERE receipt_uid=:uid',{'uid':row['receipt_uid']})
        if receipt['classification'] not in ('job_estimate','unknown') or (receipt['job_uid'],receipt['owner_scope'],receipt['owner_key'],receipt['input_sha256'])!=(row['job_uid'],row['owner_scope'],row['owner_key'],row['current_input_sha256']) or row['invalidation_reason'] is not None:_fail()
        validate_receipt(db,receipt)


def validate_schema(db):
    """Exact owned schema/executables; source registry remains frozen external."""
    pg=db.dialect.name=='postgresql';inspector=inspect(db)
    if not pg:
        if db.exec_driver_sql('PRAGMA foreign_keys').scalar_one()!=1 or db.exec_driver_sql('PRAGMA ignore_check_constraints').scalar_one()!=0:_fail()
        for table in s.TABLES:
            actual=db.execute(text("SELECT sql FROM sqlite_schema WHERE type='table' AND name=:name"),{'name':table}).scalar_one_or_none()
            if actual!=s.ddl(table):_fail()
            if db.execute(text("SELECT 1 FROM sqlite_schema WHERE type='index' AND tbl_name=:name AND sql IS NOT NULL LIMIT 1"),{'name':table}).first():_fail()
            actual_triggers=dict(db.execute(text("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=:name"),{'name':table}).all())
            if actual_triggers!={n:sql for n,sql in s.sqlite_triggers().items() if n.startswith(table+'_')}:_fail()
            if db.exec_driver_sql('PRAGMA foreign_key_check('+table+')').first():_fail()
        return
    if db.exec_driver_sql('SELECT current_schema()').scalar_one()!='public':_fail()
    for table in s.TABLES:
        identity=db.execute(text("SELECT c.oid,c.relnamespace='public'::regnamespace,c.relkind,c.relpersistence FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:name)"),{'name':'public.'+table}).one_or_none()
        if identity is None or identity[1:]!=(True,'r','p'):_fail()
        reference='priced_reference_'+table
        s.execute_closed(db,s.ddl(table,True,False).replace('CREATE TABLE '+table,'CREATE TEMPORARY TABLE '+reference,1))
        try:
            temp=db.exec_driver_sql('SELECT pg_my_temp_schema()::regnamespace::text').scalar_one()
            def shape(name,schema):
                ins=inspect(db)
                return ([{k:c.get(k) for k in ('name','type','nullable','default','identity','computed')} for c in ins.get_columns(name,schema=schema)],ins.get_pk_constraint(name,schema=schema)['constrained_columns'],sorted(tuple(c['column_names']) for c in ins.get_unique_constraints(name,schema=schema)),sorted(re.sub(r'\s+','',c['sqltext']) for c in ins.get_check_constraints(name,schema=schema)))
            actual,expected=shape(table,'public'),shape(reference,temp)
            # SQLAlchemy type instances compare by normalized representation.
            for columns in (actual[0],expected[0]):
                for col in columns:col['type']=str(col['type'])
            if actual!=expected:_fail()
            if any(not index.get('duplicates_constraint') for index in inspect(db).get_indexes(table,schema='public')):_fail()
            for kind in ('c','p','u'):
                attributes=db.execute(text("SELECT convalidated,condeferrable,condeferred FROM pg_catalog.pg_constraint WHERE conrelid=:oid AND contype=:kind"),{'oid':identity[0],'kind':kind}).all()
                if any(tuple(row)!=(True,False,False) for row in attributes):_fail()
            index_states=db.execute(text("SELECT indisvalid,indisready,indimmediate,indnullsnotdistinct,indpred IS NULL,indexprs IS NULL,indnkeyatts=indnatts FROM pg_catalog.pg_index WHERE indrelid=:oid"),{'oid':identity[0]}).all()
            if any(tuple(row)!=(True,True,True,False,True,True,True) for row in index_states):_fail()
            expected_fks=[rule for rule in s.definitions(True)[table][1] if rule.startswith('FOREIGN KEY')]
            actual_fks=db.execute(text("SELECT pg_get_constraintdef(c.oid),c.convalidated,c.confrelid FROM pg_catalog.pg_constraint c WHERE c.conrelid=:oid AND c.contype='f'"),{'oid':identity[0]}).all()
            if len(actual_fks)!=len(expected_fks):_fail()
            for rule in expected_fks:
                normalized=re.sub(r'\s+','',rule).replace('ONUPDATENOACTION','').replace('ONDELETENOACTION','')
                matched=[r for r in actual_fks if re.sub(r'\s+','',r[0]).replace('public.','')==normalized]
                if len(matched)!=1 or not matched[0][1]:_fail()
                target=re.search(r'REFERENCES (\w+)',rule)[1]
                if matched[0][2]!=db.execute(text('SELECT to_regclass(:name)::oid'),{'name':'public.'+target}).scalar_one():_fail()
        finally:db.exec_driver_sql('DROP TABLE '+reference)
    for name,body in s.pg_functions().items():
        rows=db.execute(text("SELECT p.prosrc,p.prosecdef,p.proconfig,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.pronargs,p.pronargdefaults,p.procost,p.prorows,p.prosupport::text,pg_get_function_result(p.oid),l.lanname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace JOIN pg_catalog.pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname=:name"),{'name':name}).all()
        if rows!=[(body,False,['search_path=pg_catalog, public'],'v','u',False,False,'f',False,0,0,100.0,0.0,'-','trigger','plpgsql')]:_fail()

    actual=db.execute(text("SELECT t.tgname,pg_get_triggerdef(t.oid),t.tgenabled FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid WHERE c.relnamespace='public'::regnamespace AND c.relname=ANY(:tables) AND NOT t.tgisinternal"),{'tables':list(s.TABLES)}).all()
    if len(actual)!=14 or {r[0] for r in actual}!=set(s.pg_triggers()) or any(r[2]!='O' for r in actual):_fail()
    for name,sql in s.pg_triggers().items():
        row=next(r for r in actual if r[0]==name)
        table=next(t for t in s.TABLES if name.startswith(t+'_'))
        attributes=db.execute(text("SELECT tgnargs,tgqual IS NULL,tgattr::text,tgconstraint,tgdeferrable,tginitdeferred,encode(tgargs,'hex'),tgfoid FROM pg_catalog.pg_trigger WHERE tgrelid=to_regclass(:table) AND tgname=:name"),{'table':'public.'+table,'name':name}).one()
        expected_oid=db.execute(text('SELECT to_regprocedure(:name)::oid'),{'name':'public.odin_'+table+'_guard()'}).scalar_one()
        if tuple(attributes)!=(0,True,'',0,False,False,'',expected_oid):_fail()

        if re.sub(r'\s+','',row[1]).replace('public.','')!=re.sub(r'\s+','',sql).replace('public.',''):_fail()


def validate(db):validate_schema(db);validate_data(db)
