"""Unregistered prospective estimate storage. No request authority or commits."""
import re
from sqlalchemy import text
from core.schema.migrator import MigrationError
from core.priced_job_estimate import CURRENCIES,FIELDS

SOURCE='priced_source_revisions';SELECTION_HISTORY='priced_selection_history';SELECTION='priced_selections'
RECEIPT='job_estimate_receipts';COMPONENT='job_estimate_components'
REFERENCE_HISTORY='job_estimate_reference_history';CURRENT='job_estimate_current'
TABLES=(SOURCE,SELECTION_HISTORY,SELECTION,RECEIPT,COMPONENT,REFERENCE_HISTORY,CURRENT)
MAX_HISTORY=4096;MAX_INT=2**63-1
NAMES=('total','subtotal','overhead','failure','base','other','packaging','depreciation','electricity','labor','material')
PREDECESSOR=dict(zip(NAMES,NAMES[1:]+NAMES[:1]))
ACTIVE_FIELDS=tuple(x for x in FIELDS if x not in ('spool_cost','spool_weight'))
SOURCE_LINK=('source_uid','source_revision','source_owner_scope','source_owner_key','currency')
REASONS=('missing_source','missing_material','missing_geometry','missing_run_count')
INVALIDATIONS=('model_changed','file_changed','payload_changed','plate_changed','runs_changed','source_changed','explicit_clear')


def refuse():raise MigrationError('Priced estimate storage contract differs') from None

def nulls(fields):return ' AND '.join(n+' IS NULL' for n in fields)
def present(fields):return ' AND '.join(n+' IS NOT NULL' for n in fields)
def literals(values):return '('+','.join("'"+v+"'" for v in values)+')'
def hex_sql(n,size,pg):return f"{n} ~ '^[0-9a-f]{{{size}}}$'" if pg else f"typeof({n})='text' AND length({n})={size} AND {n} NOT GLOB '*[^0-9a-f]*'"
def nonce_sql(n,pg):return '('+hex_sql(n,32,pg)+f" AND substr({n},13,1)='4' AND substr({n},17,1) IN ('8','9','a','b'))"
def job_uid_sql(n,pg):
    stripped="replace("+n+",'-','')"
    return '('+nonce_sql(stripped,pg)+f" AND length({n})=36 AND substr({n},9,1)='-' AND substr({n},14,1)='-' AND substr({n},19,1)='-' AND substr({n},24,1)='-')"
def owner_sql(scope='owner_scope',key='owner_key'):return f"(({scope}='global' AND {key}=0) OR ({scope}='org' AND {key}>0))"
def decimal_sql(n,pg):
    if pg:return f"{n} ~ '^(0|[1-9][0-9]{{0,17}})([.][0-9]{{0,8}}[1-9])?$'"
    integer=f"CASE WHEN instr({n},'.')=0 THEN {n} ELSE substr({n},1,instr({n},'.')-1) END"
    return f"typeof({n})='text' AND length({n}) BETWEEN 1 AND 28 AND {n} NOT GLOB '*[^0-9.]*' AND ({integer}='0' OR (length({integer}) BETWEEN 1 AND 18 AND substr({integer},1,1) BETWEEN '1' AND '9')) AND (instr({n},'.')=0 OR (length({n})-instr({n},'.') BETWEEN 1 AND 9 AND substr({n},-1,1) BETWEEN '1' AND '9' AND instr(substr({n},instr({n},'.')+1),'.')=0))"
def uint_sql(n,pg,positive=False):
    maximum=str(2**1024-1)
    shape=f"{n} ~ '^(0|[1-9][0-9]*)$'" if pg else f"typeof({n})='text' AND length({n})>=1 AND {n} NOT GLOB '*[^0-9]*' AND ({n}='0' OR substr({n},1,1) BETWEEN '1' AND '9')"
    return f"({shape}) AND (length({n})<309 OR (length({n})=309 AND {n}<='{maximum}'))"+(f" AND {n}!='0'" if positive else '')
def time_sql(n,pg):
    if pg:return f'isfinite({n})'
    return f"typeof({n})='text' AND length({n})=27 AND substr({n},11,1)='T' AND substr({n},20,1)='.' AND substr({n},27,1)='Z' AND replace(replace(replace(replace(replace({n},'-',''),'T',''),':',''),'.',''),'Z','') NOT GLOB '*[^0-9]*' AND substr({n},1,4) BETWEEN '0001' AND '9999' AND substr({n},12,2) BETWEEN '00' AND '23' AND substr({n},15,2) BETWEEN '00' AND '59' AND substr({n},18,2) BETWEEN '00' AND '59' AND date(substr({n},1,10),'+0 days') IS substr({n},1,10)"


def definitions(pg=False):
    result={};integer='BIGINT' if pg else 'INTEGER';timestamp='TIMESTAMP WITH TIME ZONE' if pg else 'TEXT'
    def table(name):
        cols=[];rules=[];result[name]=(cols,rules)
        def column(n,t='TEXT',nullable=False,check=None):
            cols.append(n+' '+t+('' if nullable else ' NOT NULL')+(' CHECK('+((n+' IS NULL OR ('+check+')') if nullable else check)+')' if check else ''))
        def number(n,nullable=False,low=0,high=MAX_INT):column(n,integer,nullable,f'{n} BETWEEN {low} AND {high}'+('' if pg else f" AND typeof({n})='integer'"))
        def nonce(n,nullable=False):column(n,nullable=nullable,check=nonce_sql(n,pg))
        def owner():column('owner_scope',check="owner_scope IN ('global','org')");number('owner_key');rules.append('CHECK('+owner_sql()+')')
        def revision(n='revision'):number(n,low=1,high=MAX_HISTORY)
        def digest(n,nullable=False):column(n,nullable=nullable,check=hex_sql(n,64,pg))
        def created():column('created_at',timestamp,check=time_sql('created_at',pg))
        def bounded_json(n):column(n,check=(f'octet_length({n}) BETWEEN 2 AND 65536' if pg else f"typeof({n})='text' AND length(CAST({n} AS BLOB)) BETWEEN 2 AND 65536 AND json_valid({n})"))
        def source_link():
            nonce('source_uid',True);number('source_revision',True,1,MAX_HISTORY);column('source_owner_scope',nullable=True,check="source_owner_scope IN ('global','org')");number('source_owner_key',True);column('currency',nullable=True,check='currency IN '+literals(CURRENCIES))
            rules.append('CHECK(('+nulls(SOURCE_LINK)+') OR ('+present(SOURCE_LINK)+' AND '+owner_sql('source_owner_scope','source_owner_key')+'))')
            rules.append('FOREIGN KEY('+','.join(SOURCE_LINK)+') REFERENCES '+SOURCE+'(source_uid,revision,owner_scope,owner_key,currency) ON DELETE RESTRICT')
        def command():nonce('actor_binding_uid');number('actor_id',True,1);nonce('command_nonce');digest('request_sha256');number('expected_revision',high=MAX_HISTORY-1);rules.append('UNIQUE(actor_binding_uid,command_nonce)')
        return column,number,nonce,owner,revision,digest,created,bounded_json,source_link,command,rules
    col,num,nonce,owner,rev,digest,created,jdoc,_,_,r=table(SOURCE)
    nonce('source_uid');rev();owner();col('currency',check='currency IN '+literals(CURRENCIES));jdoc('assumptions_json');digest('source_sha256');col('assumptions_kind',check="assumptions_kind IN ('explicit_saved','declared_hypothetical')");num('actor_id',True,1);created();r.extend(['PRIMARY KEY(source_uid,revision)','UNIQUE(source_uid,revision,owner_scope,owner_key,currency)'])
    col,num,nonce,owner,rev,digest,created,_,link,command,r=table(SELECTION_HISTORY)
    owner();rev();link();command();created();r.extend(['PRIMARY KEY(owner_scope,owner_key,revision)',"CHECK(source_uid IS NULL OR (source_owner_scope='global' AND source_owner_key=0) OR (owner_scope='org' AND source_owner_scope='org' AND source_owner_key=owner_key))"])
    col,num,nonce,owner,rev,digest,created,_,_,_,r=table(SELECTION)
    owner();rev();r.extend(['PRIMARY KEY(owner_scope,owner_key)','FOREIGN KEY(owner_scope,owner_key,revision) REFERENCES '+SELECTION_HISTORY+'(owner_scope,owner_key,revision) ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED'])
    col,num,nonce,owner,rev,digest,created,jdoc,link,_,r=table(RECEIPT)
    nonce('receipt_uid');col('job_uid',check=job_uid_sql('job_uid',pg));col('job_kind',check="job_kind='job'");owner();rev('receipt_revision');col('classification',check="classification IN ('job_estimate','scenario','unknown')");col('formula',check="formula='per-run-cost-v1'");col('rounding',check="rounding='HALF_EVEN'");link();col('amount',nullable=True);num('runs',True,1,10000);col('required_total_name',nullable=True);col('unknown_reason',nullable=True,check='unknown_reason IN '+literals(REASONS));col('selection_owner_scope',nullable=True);num('selection_owner_key',True);num('selection_revision',True,1,MAX_HISTORY);digest('input_sha256');jdoc('input_json');digest('payload_sha256',True);num('plate_index',True);num('piece_count',True,1);num('capacity',True,1);created()
    known=('amount','runs','required_total_name','selection_owner_scope','selection_owner_key','selection_revision',*SOURCE_LINK)
    r.extend(['PRIMARY KEY(receipt_uid)','UNIQUE(job_uid,receipt_revision)','UNIQUE(receipt_uid,currency)','UNIQUE(receipt_uid,job_uid,owner_scope,owner_key)',
              'FOREIGN KEY(job_uid,job_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT',
              'FOREIGN KEY(selection_owner_scope,selection_owner_key,selection_revision) REFERENCES '+SELECTION_HISTORY+'(owner_scope,owner_key,revision) ON DELETE RESTRICT',
              'FOREIGN KEY(receipt_uid,required_total_name) REFERENCES '+COMPONENT+'(receipt_uid,component_name) ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED',
              "CHECK((classification IN ('job_estimate','scenario') AND "+present(known)+" AND required_total_name='total' AND unknown_reason IS NULL AND selection_owner_scope=owner_scope AND selection_owner_key=owner_key) OR (classification='unknown' AND "+nulls(known)+" AND unknown_reason IS NOT NULL))"])
    money=(f"amount ~ '^(0|[1-9][0-9]{{0,17}})$'" if pg else f"{uint_sql('amount',False)} AND length(amount)<=18")
    money2=(f"amount ~ '^(0|[1-9][0-9]{{0,17}})[.][0-9]{{2}}$'" if pg else "typeof(amount)='text' AND instr(amount,'.') BETWEEN 2 AND 19 AND length(amount)-instr(amount,'.')=2 AND amount NOT GLOB '*[^0-9.]*' AND instr(substr(amount,instr(amount,'.')+1),'.')=0 AND (substr(amount,1,instr(amount,'.')-1)='0' OR substr(amount,1,1) BETWEEN '1' AND '9')")
    r.append("CHECK(amount IS NULL OR ((currency='JPY' AND "+money+") OR (currency!='JPY' AND "+money2+')) )')
    col,num,nonce,owner,rev,digest,created,_,_,_,r=table(COMPONENT)
    nonce('receipt_uid');col('component_name',check='component_name IN '+literals(NAMES));col('currency',check='currency IN '+literals(CURRENCIES));col('basis',check="basis IN ('per_run','all_runs')");col('predecessor_name');col('numerator',check=uint_sql('numerator',pg));col('denominator',check=uint_sql('denominator',pg,True));r.extend(['PRIMARY KEY(receipt_uid,component_name)',
        'FOREIGN KEY(receipt_uid,currency) REFERENCES '+RECEIPT+'(receipt_uid,currency) ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED',
        'FOREIGN KEY(receipt_uid,predecessor_name) REFERENCES '+COMPONENT+'(receipt_uid,component_name) ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED',
        "CHECK((component_name='total' AND basis='all_runs') OR (component_name!='total' AND basis='per_run'))",
        'CHECK('+' OR '.join("(component_name='"+n+"' AND predecessor_name='"+PREDECESSOR[n]+"')" for n in NAMES)+')'])
    for name in (REFERENCE_HISTORY,CURRENT):
        col,num,nonce,owner,rev,digest,created,_,_,command,r=table(name)
        col('job_uid',check=job_uid_sql('job_uid',pg));owner();rev();nonce('receipt_uid',True);col('invalidation_reason',nullable=True,check='invalidation_reason IN '+literals(INVALIDATIONS));digest('current_input_sha256',True)
        r.extend(['PRIMARY KEY(job_uid'+(',revision' if name==REFERENCE_HISTORY else '')+')',
                  'FOREIGN KEY(receipt_uid,job_uid,owner_scope,owner_key) REFERENCES '+RECEIPT+'(receipt_uid,job_uid,owner_scope,owner_key) ON DELETE RESTRICT',
                  "CHECK((receipt_uid IS NOT NULL AND invalidation_reason IS NULL AND current_input_sha256 IS NOT NULL) OR (receipt_uid IS NULL AND invalidation_reason IS NOT NULL AND current_input_sha256 IS NULL))"])
        if name==REFERENCE_HISTORY:command();created()
        else:r.append('FOREIGN KEY(job_uid,revision) REFERENCES '+REFERENCE_HISTORY+'(job_uid,revision) ON UPDATE NO ACTION ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED')
    return result


def ddl(name,pg=False,foreign=True):
    cols,rules=definitions(pg)[name]
    return 'CREATE TABLE '+name+'('+','.join(cols+[r for r in rules if foreign or not r.startswith('FOREIGN KEY')])+')'


def guard_predicates():
    """Invalid INSERT predicates, with NEW and portable null-safe comparison."""
    result={}
    result[SOURCE]=f"NEW.revision!=(SELECT COALESCE(MAX(revision),0)+1 FROM {SOURCE} WHERE source_uid=NEW.source_uid) OR EXISTS(SELECT 1 FROM {SOURCE} WHERE source_uid=NEW.source_uid AND (owner_scope IS DISTINCT FROM NEW.owner_scope OR owner_key IS DISTINCT FROM NEW.owner_key))"
    result[SELECTION_HISTORY]=f"NEW.revision!=(SELECT COALESCE(MAX(revision),0)+1 FROM {SELECTION_HISTORY} WHERE owner_scope=NEW.owner_scope AND owner_key=NEW.owner_key) OR NEW.expected_revision!=NEW.revision-1"
    result[RECEIPT]=f"NEW.receipt_revision!=(SELECT COALESCE(MAX(receipt_revision),0)+1 FROM {RECEIPT} WHERE job_uid=NEW.job_uid) OR (NEW.classification!='unknown' AND NOT EXISTS(SELECT 1 FROM {SELECTION_HISTORY} h WHERE h.owner_scope=NEW.selection_owner_scope AND h.owner_key=NEW.selection_owner_key AND h.revision=NEW.selection_revision AND "+' AND '.join('h.'+n+' IS NOT DISTINCT FROM NEW.'+n for n in SOURCE_LINK)+'))'
    result[COMPONENT]=f"EXISTS(SELECT 1 FROM {RECEIPT} WHERE receipt_uid=NEW.receipt_uid AND (classification='unknown' OR currency IS DISTINCT FROM NEW.currency))"
    result[REFERENCE_HISTORY]=f"NEW.revision!=(SELECT COALESCE(MAX(revision),0)+1 FROM {REFERENCE_HISTORY} WHERE job_uid=NEW.job_uid) OR NEW.expected_revision!=NEW.revision-1 OR "+reference_invalid('NEW')
    result[SELECTION]=f"NEW.revision!=1 OR EXISTS(SELECT 1 FROM {SELECTION_HISTORY} WHERE owner_scope=NEW.owner_scope AND owner_key=NEW.owner_key AND revision>1)"
    result[CURRENT]=f"NEW.revision!=1 OR EXISTS(SELECT 1 FROM {REFERENCE_HISTORY} WHERE job_uid=NEW.job_uid AND revision>1) OR "+reference_invalid('NEW')+' OR NOT EXISTS(SELECT 1 FROM '+REFERENCE_HISTORY+' h WHERE h.job_uid=NEW.job_uid AND h.revision=NEW.revision AND '+' AND '.join('h.'+n+' IS NOT DISTINCT FROM NEW.'+n for n in ('owner_scope','owner_key','receipt_uid','invalidation_reason','current_input_sha256'))+')'
    for table in (REFERENCE_HISTORY,CURRENT):
        result[table]+=' OR NOT EXISTS(SELECT 1 FROM cost_identities WHERE identity_uid=NEW.job_uid AND resource_kind=\'job\' AND owner_scope=NEW.owner_scope AND owner_key=NEW.owner_key)'
    return result


def reference_invalid(prefix):
    return f"({prefix}.receipt_uid IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {RECEIPT} r WHERE r.receipt_uid={prefix}.receipt_uid AND r.job_uid={prefix}.job_uid AND r.owner_scope={prefix}.owner_scope AND r.owner_key={prefix}.owner_key AND r.classification IN ('job_estimate','unknown') AND r.input_sha256={prefix}.current_input_sha256))"

def update_invalid(table):
    if table==SELECTION:
        return "NEW.owner_scope IS DISTINCT FROM OLD.owner_scope OR NEW.owner_key IS DISTINCT FROM OLD.owner_key OR NEW.revision!=OLD.revision+1 OR NOT EXISTS(SELECT 1 FROM "+SELECTION_HISTORY+" h WHERE h.owner_scope=NEW.owner_scope AND h.owner_key=NEW.owner_key AND h.revision=NEW.revision AND h.expected_revision=OLD.revision) OR EXISTS(SELECT 1 FROM "+SELECTION_HISTORY+" WHERE owner_scope=NEW.owner_scope AND owner_key=NEW.owner_key AND revision>NEW.revision)"
    fields=('job_uid','owner_scope','owner_key')
    return ' OR '.join('NEW.'+n+' IS DISTINCT FROM OLD.'+n for n in fields)+" OR NEW.revision!=OLD.revision+1 OR NOT EXISTS(SELECT 1 FROM "+REFERENCE_HISTORY+" h WHERE h.job_uid=NEW.job_uid AND h.revision=NEW.revision AND h.expected_revision=OLD.revision AND "+' AND '.join('h.'+n+' IS NOT DISTINCT FROM NEW.'+n for n in (*fields,'receipt_uid','invalidation_reason','current_input_sha256'))+') OR EXISTS(SELECT 1 FROM '+REFERENCE_HISTORY+' WHERE job_uid=NEW.job_uid AND revision>NEW.revision) OR '+reference_invalid('NEW')


def conflict(table):
    keys={SOURCE:('source_uid','revision'),SELECTION_HISTORY:('owner_scope','owner_key','revision'),SELECTION:('owner_scope','owner_key'),RECEIPT:('receipt_uid',),COMPONENT:('receipt_uid','component_name'),REFERENCE_HISTORY:('job_uid','revision'),CURRENT:('job_uid',)}[table]
    tests=['EXISTS(SELECT 1 FROM '+table+' WHERE '+' AND '.join(n+'=NEW.'+n for n in keys)+')']
    if table in (SELECTION_HISTORY,REFERENCE_HISTORY):tests.append(f'EXISTS(SELECT 1 FROM {table} WHERE actor_binding_uid=NEW.actor_binding_uid AND command_nonce=NEW.command_nonce)')
    if table==RECEIPT:tests.append(f'EXISTS(SELECT 1 FROM {table} WHERE job_uid=NEW.job_uid AND receipt_revision=NEW.receipt_revision)')
    return ' OR '.join(tests)


def source_json_invalid(pg):
    doc="NEW.assumptions_json"
    if pg:
        obj=doc+'::jsonb'; assumption=obj+"->'assumptions'"; rates=obj+"->'material_rates'"
        kind=lambda x:"jsonb_typeof("+x+")"
        count=lambda x:"(SELECT count(*) FROM jsonb_object_keys("+x+"))"
        value=lambda k:assumption+"->>'"+k+"'"
        istext=lambda k:kind(assumption+"->'"+k+"'")+"='string'"
        outer_currency=obj+"->>'currency'"
        rate_rows="jsonb_each("+rates+") q"
        rate_value="q.value->>'cost_per_gram'"
        rate_type=kind("q.value->'cost_per_gram'")+"='string'"
        rate_good=kind('q.value')+"='object' AND "+count('q.value')+"=2 AND q.value->>'currency'=NEW.currency AND "+rate_type+' AND '+decimal_sql(rate_value,True)
        material_key="q.key ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'"
    else:
        assumption="json_extract("+doc+",'$.assumptions')";rates="json_extract("+doc+",'$.material_rates')";obj=doc
        kind=lambda x:"json_type("+x+")"
        count=lambda x:"(SELECT count(*) FROM json_each("+x+"))"
        value=lambda k:"json_extract("+doc+",'$.assumptions."+k+"')"
        istext=lambda k:"json_type("+doc+",'$.assumptions."+k+"')='text'"
        outer_currency="json_extract("+doc+",'$.currency')"
        rate_rows="json_each("+rates+") q";rate_value="json_extract(q.value,'$.cost_per_gram')"
        rate_type="json_type(q.value,'$.cost_per_gram')='text'"
        rate_good="q.type='object' AND "+count('q.value')+"=2 AND json_extract(q.value,'$.currency')=NEW.currency AND "+rate_type+' AND '+decimal_sql(rate_value,False)
        material_key="length(q.key) BETWEEN 1 AND 64 AND substr(q.key,1,1) GLOB '[A-Za-z0-9]' AND q.key NOT GLOB '*[^A-Za-z0-9_.-]*'"
    terms=[kind(obj)+"='object'",count(obj)+'=3',outer_currency+'=NEW.currency',kind(assumption)+"='object'",count(assumption)+'=13',kind(rates)+"='object'",count(rates)+'<=128']
    for field in ACTIVE_FIELDS:
        expression=value(field);terms.extend((istext(field),decimal_sql(expression,pg)))
        if field in ('printer_lifespan','parts_per_month'):terms.append(expression+"!='0'")
        if field=='failure_rate':terms.append('CAST('+expression+' AS NUMERIC)<=10000')
    terms.append('NOT EXISTS(SELECT 1 FROM '+rate_rows+' WHERE ('+material_key+' AND '+rate_good+') IS NOT TRUE)')
    return '('+' AND '.join(terms)+') IS NOT TRUE'


def sqlite_triggers():
    result={}
    for table in TABLES:
        for event,condition in (('insert',conflict(table)+' OR '+guard_predicates()[table]+(' OR '+source_json_invalid(False) if table==SOURCE else '')),('update',update_invalid(table) if table in (SELECTION,CURRENT) else '1'),('delete','1')):
            name=table+'_'+event+'_guard';result[name]=f"CREATE TRIGGER {name} BEFORE {event.upper()} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'priced estimate transition refused'); END"
    return result


def pg_functions():
    result={}
    for table in TABLES:
        # Independent direct-insert sequence serialization: a transaction-scoped
        # advisory lock is acquired before reading MAX, including absent histories.
        key={SOURCE:'NEW.source_uid',SELECTION_HISTORY:"NEW.owner_scope||':'||NEW.owner_key::text",RECEIPT:'NEW.job_uid',REFERENCE_HISTORY:'NEW.job_uid'}.get(table)
        lock='' if key is None else f"IF NOT pg_try_advisory_xact_lock(hashtextextended('{table}:'||{key},0)) THEN RAISE EXCEPTION 'priced estimate sequence busy' USING ERRCODE='55P03'; END IF;"
        body=f"BEGIN IF TG_OP='TRUNCATE' OR TG_OP='DELETE' THEN RAISE EXCEPTION 'priced estimate history retained' USING ERRCODE='23514'; ELSIF TG_OP='UPDATE' THEN IF {update_invalid(table) if table in (SELECTION,CURRENT) else 'TRUE'} THEN RAISE EXCEPTION 'priced estimate update refused' USING ERRCODE='23514'; END IF; ELSE {lock} IF {conflict(table)} OR {guard_predicates()[table]} {' OR '+source_json_invalid(True) if table==SOURCE else ''} THEN RAISE EXCEPTION 'priced estimate insert refused' USING ERRCODE='23514'; END IF; END IF; RETURN NEW; END;"
        # Fixed public relation references, never a caller search_path fallback.
        body=re.sub(r'(?<=FROM )('+ '|'.join((*TABLES,'cost_identities'))+r')\b',r'public.\1',body)
        result['odin_'+table+'_guard']=body
    return result


def pg_triggers():
    return {table+'_'+event+'_guard':f'CREATE TRIGGER {table}_{event}_guard BEFORE '+('TRUNCATE' if event=='truncate' else 'INSERT OR DELETE OR UPDATE')+' ON public.'+table+' FOR EACH '+('STATEMENT' if event=='truncate' else 'ROW')+' EXECUTE FUNCTION public.odin_'+table+'_guard()' for table in TABLES for event in ('row','truncate')}


def execute_closed(connection,sql):
    clause=text(sql);compiled=clause.compile(dialect=connection.dialect)
    if compiled.params or compiled.binds:refuse()
    return connection.execute(clause)


def apply(connection):
    """Install in an already active caller transaction; never owns COMMIT."""
    if not connection.in_transaction():refuse()
    pg=connection.dialect.name=='postgresql'
    if not pg and connection.dialect.name!='sqlite':refuse()
    if pg:
        if connection.exec_driver_sql('SHOW transaction_isolation').scalar_one()!='read committed' or connection.exec_driver_sql('SELECT current_schema()').scalar_one()!='public':refuse()
        bound=int(connection.exec_driver_sql("SELECT setting FROM pg_settings WHERE name='lock_timeout'").scalar_one())
        if bound==0 or bound>2000:connection.exec_driver_sql("SELECT set_config('lock_timeout','2000ms',true)")
    else:
        if not connection.connection.driver_connection.in_transaction or connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one()!=1 or connection.exec_driver_sql('PRAGMA ignore_check_constraints').scalar_one()!=0:refuse()
    from sqlalchemy import inspect
    inspector=inspect(connection)
    existing=set(inspector.get_table_names(schema='public' if pg else None))
    if 'cost_identities' not in existing:refuse()
    if existing.intersection(TABLES):
        if not set(TABLES)<=existing:refuse()
        from core.priced_estimate_validation import validate
        validate(connection);return
    for table in TABLES:execute_closed(connection,ddl(table,pg,foreign=not pg).replace('CREATE TABLE '+table,'CREATE TABLE public.'+table,1) if pg else ddl(table))
    if pg:
        for table in TABLES:
            for rule in definitions(True)[table][1]:
                if rule.startswith('FOREIGN KEY'):execute_closed(connection,'ALTER TABLE public.'+table+' ADD '+re.sub(r'REFERENCES (\w+)',r'REFERENCES public.\1',rule))
        for name,body in pg_functions().items():execute_closed(connection,'CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$'+body+'$$')
        for sql in pg_triggers().values():execute_closed(connection,sql)
    else:
        for sql in sqlite_triggers().values():execute_closed(connection,sql)
