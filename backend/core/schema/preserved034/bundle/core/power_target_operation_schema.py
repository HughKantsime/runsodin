"""027 storage only. No command, authorization, recovery or internal commit."""
from __future__ import annotations
import re
import sqlite3
from datetime import datetime, timezone
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from core.schema.migrator import MigrationError

TABLE = 'power_target_operations'
FUNCTION = 'power_target_operation_guard'
MIGRATION_ID = 'python:027-power-target-operations'
MAX = 9223372036854775807
IDENTITY = ('operation_nonce','request_nonce','owner_nonce','printer_id','target_key','config_key','destination_key','purpose','physical_job_id','scheduled_job_id','observation_key','actor_user_id','auth_kind','authority_key','created_at')
PHASE = ('command_started_at','release_pending_at','completed_at','outcome')
COLUMNS = (*IDENTITY[:-1], 'state','revision','created_at','changed_at',*PHASE[:3],'outcome','cleanup_reason')
TIMES = ('created_at','changed_at',*PHASE[:3])
INDEXES = {'ptop_active_printer':'printer_id','ptop_active_target':'target_key','ptop_active_destination':'destination_key'}


def connection(executor):
    if isinstance(executor, Session):
        if not executor.in_transaction(): raise MigrationError('Power storage caller transaction required')
        executor = executor.connection()
    if isinstance(executor, Connection):
        if not executor.in_transaction(): raise MigrationError('Power storage caller transaction required')
        return executor
    raw = getattr(executor, '_connection', executor)
    driver = getattr(raw, 'driver_connection', raw)
    if hasattr(driver, 'info'):
        if driver.autocommit or int(driver.info.transaction_status) != 2: raise MigrationError('Power storage caller transaction required')
    elif not isinstance(driver, sqlite3.Connection) or not driver.in_transaction:
        raise MigrationError('Power storage caller transaction required')
    return executor


def dialect(executor):
    c = connection(executor)
    if isinstance(c, Connection): name = c.dialect.name
    else:
        raw = getattr(c, '_connection', c); driver = getattr(raw, 'driver_connection', raw)
        name = 'postgresql' if hasattr(driver, 'info') else 'sqlite'
    if name not in ('postgresql','sqlite'): raise MigrationError('Unsupported power storage dialect')
    return name


def execute(executor, sql, values=()):
    c = connection(executor)
    if dialect(c) == 'postgresql': sql = sql.replace('%','%%')
    if isinstance(c, Connection):
        if c.dialect.name == 'postgresql':
            from core.db_utils import _postgres_placeholders
            sql = _postgres_placeholders(sql)
        return c.exec_driver_sql(sql, tuple(values))
    return c.execute(sql, tuple(values))


def one(c, sql, values=()): return execute(c, sql, values).fetchone()
def rows(c, sql, values=()): return [tuple(r) for r in execute(c, sql, values).fetchall()]


def tokens(sql):
    # Preserve all literals/quoted identifiers and order; comments are not normalized away.
    result = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", str(sql))
    if result and result[-1] == ';': result.pop()
    return result


def hexcheck(n, width, pg):
    return f"pg_catalog.octet_length({n})={width} AND {n} ~ '^[0-9a-f]{{{width}}}$'" if pg else f"typeof({n})='text' AND length({n})={width} AND {n} NOT GLOB '*[^0-9a-f]*'"


def checks(pg):
    result = {}
    for n in ('operation_nonce','request_nonce','owner_nonce','target_key','config_key','destination_key','observation_key','authority_key'):
        check = hexcheck(n, 32 if n.endswith('nonce') else 64, pg)
        if n in ('request_nonce','observation_key','authority_key'): check = f'{n} IS NULL OR ({check})'
        result['ptop_'+n] = check
    for n in ('printer_id','physical_job_id','scheduled_job_id','actor_user_id','revision'):
        check = f'{n}>={0 if n=="revision" else 1} AND {n}<={MAX}'
        if not pg: check += f" AND typeof({n})='integer'"
        if n not in ('printer_id','revision'): check = f'{n} IS NULL OR ({check})'
        result['ptop_'+n] = check
    result['ptop_authority'] = "(purpose IN ('observed_on','cooldown_off') AND physical_job_id IS NOT NULL AND observation_key IS NOT NULL AND request_nonce IS NULL AND actor_user_id IS NULL AND auth_kind IS NULL AND authority_key IS NULL) OR (purpose IN ('manual_on','manual_off','manual_toggle') AND request_nonce IS NOT NULL AND actor_user_id IS NOT NULL AND auth_kind IN ('session_jwt','bearer_jwt','scoped_api_key','legacy_global_api_key') AND authority_key IS NOT NULL AND physical_job_id IS NULL AND scheduled_job_id IS NULL AND observation_key IS NULL)"
    # Explicit auth_kind IS NOT NULL closes SQL UNKNOWN in the manual branch.
    result['ptop_authority'] = result['ptop_authority'].replace("AND auth_kind IN", "AND auth_kind IS NOT NULL AND auth_kind IN")
    result['ptop_outcome'] = "outcome IS NULL OR outcome IN ('on_observed','off_observed','not_sent')"
    result['ptop_reason'] = "cleanup_reason IS NULL OR cleanup_reason IN ('commit_uncertain','start_uncertain','command_uncertain','worker_uncertain','ipc_uncertain','target_uncertain','authority_drift','storage_unavailable')"
    result['ptop_state'] = "(state='preparing' AND command_started_at IS NULL AND release_pending_at IS NULL AND completed_at IS NULL AND outcome IS NULL AND cleanup_reason IS NULL) OR (state='command_started' AND command_started_at IS NOT NULL AND release_pending_at IS NULL AND completed_at IS NULL AND outcome IS NULL AND cleanup_reason IS NULL) OR (state='release_pending' AND release_pending_at IS NOT NULL AND completed_at IS NULL AND outcome IS NOT NULL AND cleanup_reason IS NULL AND (outcome='not_sent' OR command_started_at IS NOT NULL)) OR (state='complete' AND release_pending_at IS NOT NULL AND completed_at IS NOT NULL AND outcome IS NOT NULL AND cleanup_reason IS NULL AND (outcome='not_sent' OR command_started_at IS NOT NULL)) OR (state='cleanup_uncertain' AND completed_at IS NULL AND cleanup_reason IS NOT NULL AND ((release_pending_at IS NULL AND outcome IS NULL) OR (release_pending_at IS NOT NULL AND outcome IS NOT NULL AND (outcome='not_sent' OR command_started_at IS NOT NULL))))"
    timechecks = []
    for n in TIMES:
        value = f'pg_catalog.isfinite({n})' if pg else f"typeof({n})='text' AND length({n})=24 AND strftime('%Y-%m-%dT%H:%M:%fZ',{n}) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ',{n})={n}"
        timechecks.append(value if n in ('created_at','changed_at') else f'({n} IS NULL OR ({value}))')
    result['ptop_finite'] = ' AND '.join('('+v+')' for v in timechecks)
    order = ['changed_at>=created_at']
    for n in PHASE[:3]: order.append(f'({n} IS NULL OR ({n}>=created_at AND {n}<=changed_at))')
    order += ['(command_started_at IS NULL OR release_pending_at IS NULL OR release_pending_at>=command_started_at)','(completed_at IS NULL OR completed_at>=release_pending_at)']
    result['ptop_time_order'] = ' AND '.join(order)
    return result


def ddl(pg, *, reference=False):
    table = '_power_target_reference' if reference else TABLE
    qualifier = '' if not pg or reference else 'public.'
    prefix = 'CREATE TEMPORARY TABLE' if reference else 'CREATE TABLE'
    integer = 'BIGINT' if pg else 'INTEGER'
    timestamp = 'TIMESTAMP WITH TIME ZONE' if pg else 'TEXT'
    default = 'pg_catalog.clock_timestamp()' if pg else "(strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    definitions = []
    required = {'operation_nonce','owner_nonce','printer_id','target_key','config_key','destination_key','purpose','state','revision','created_at','changed_at'}
    for n in COLUMNS:
        typ = integer if n in ('printer_id','physical_job_id','scheduled_job_id','actor_user_id','revision') else timestamp if n in TIMES else 'TEXT'
        definition = n+' '+typ+(' NOT NULL' if n in required else '')
        if n in ('created_at','changed_at'): definition += ' DEFAULT '+default
        definitions.append(definition)
    definitions += ['CONSTRAINT ptop_operation_pk PRIMARY KEY(operation_nonce)','CONSTRAINT ptop_request_unique UNIQUE(request_nonce)']
    definitions += ['CONSTRAINT '+n+' CHECK('+sql+')' for n,sql in checks(pg).items()]
    return prefix+' '+qualifier+table+' ('+','.join(definitions)+')'


def indexes(pg, *, reference=False):
    table = '_power_target_reference' if reference else ('public.' if pg else '')+TABLE
    return {n: f"CREATE UNIQUE INDEX {'ref_'+n if reference else n} ON {table}({col}) WHERE state<>'complete'" for n,col in INDEXES.items()}


def invalid_update(pg):
    different = 'IS DISTINCT FROM' if pg else 'IS NOT'
    same = 'IS NOT DISTINCT FROM' if pg else 'IS'
    changes = [f'NEW.{n} {different} OLD.{n}' for n in IDENTITY]
    changes += ['NEW.revision<>OLD.revision+1','NEW.changed_at<OLD.changed_at',"NOT ((OLD.state='preparing' AND NEW.state IN ('command_started','release_pending','cleanup_uncertain')) OR (OLD.state='command_started' AND NEW.state IN ('release_pending','cleanup_uncertain')) OR (OLD.state='release_pending' AND NEW.state IN ('complete','cleanup_uncertain')))"]
    for n in PHASE:
        changes.append(f'(OLD.{n} IS NOT NULL AND NEW.{n} {different} OLD.{n})')
    for n,state in (('command_started_at','command_started'),('release_pending_at','release_pending'),('completed_at','complete'),('outcome','release_pending')):
        changes.append(f'(NEW.{n} {different} OLD.{n} AND NEW.state<>\'{state}\')')
    changes.append("(OLD.state='preparing' AND NEW.state='release_pending' AND NEW.outcome<>'not_sent')")
    changes.append("(NEW.state='cleanup_uncertain' AND NOT ("+' AND '.join(f'NEW.{n} {same} OLD.{n}' for n in PHASE)+'))')
    return ' OR '.join('('+v+')' for v in changes)


def initial(): return "NEW.state<>'preparing' OR NEW.revision<>0 OR "+' OR '.join('NEW.'+n+' IS NOT NULL' for n in (*PHASE,'cleanup_reason'))


def sqlite_triggers():
    collisions = f"EXISTS(SELECT 1 FROM {TABLE} WHERE operation_nonce=NEW.operation_nonce OR (NEW.request_nonce IS NOT NULL AND request_nonce=NEW.request_nonce) OR (state<>'complete' AND (printer_id=NEW.printer_id OR target_key=NEW.target_key OR destination_key=NEW.destination_key)))"
    return {
        'ptop_insert_guard':f"CREATE TRIGGER ptop_insert_guard BEFORE INSERT ON {TABLE} BEGIN SELECT CASE WHEN {initial()} OR {collisions} THEN RAISE(ABORT,'power operation must be fresh') END; END",
        'ptop_update_guard':f"CREATE TRIGGER ptop_update_guard BEFORE UPDATE ON {TABLE} BEGIN SELECT CASE WHEN {invalid_update(False)} THEN RAISE(ABORT,'power operation transition differs') END; END",
        'ptop_delete_guard':f"CREATE TRIGGER ptop_delete_guard BEFORE DELETE ON {TABLE} BEGIN SELECT RAISE(ABORT,'power operation history is retained'); END",
    }


def pg_functions():
    return {FUNCTION: "BEGIN IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'power operation history is retained' USING ERRCODE='23514'; END IF; IF TG_OP='INSERT' THEN IF "+initial()+" THEN RAISE EXCEPTION 'power operation must be fresh' USING ERRCODE='23514'; END IF; ELSIF "+invalid_update(True)+" THEN RAISE EXCEPTION 'power operation transition differs' USING ERRCODE='23514'; END IF; RETURN NEW; END"}


def pg_triggers():
    return {
        'power_target_operations_row_guard':f'CREATE TRIGGER power_target_operations_row_guard BEFORE INSERT OR UPDATE OR DELETE ON public.{TABLE} FOR EACH ROW EXECUTE FUNCTION public.{FUNCTION}()',
        'power_target_operations_no_truncate':f'CREATE TRIGGER power_target_operations_no_truncate BEFORE TRUNCATE ON public.{TABLE} FOR EACH STATEMENT EXECUTE FUNCTION public.{FUNCTION}()',
    }


def boundary(c, *, adoption=False):
    pg = dialect(c)=='postgresql'
    if pg:
        if one(c,'SELECT current_schema()')[0]!='public': raise MigrationError('Power schema differs')
        # Existing caller's shared schema-writer lock is retained; finite local acquisition.
        execute(c,"SELECT set_config('lock_timeout', CASE WHEN current_setting('lock_timeout')::interval > interval '0 milliseconds' AND current_setting('lock_timeout')::interval <= interval '2000 milliseconds' THEN current_setting('lock_timeout') ELSE '2000ms' END, true)")
        objects = rows(c,"SELECT n.nspname,p.relkind,p.relpersistence FROM pg_class p JOIN pg_namespace n ON n.oid=p.relnamespace WHERE p.relname=?",(TABLE,))
        if objects and objects != [('public','r','p')]: raise MigrationError('Power relation shadowed or differs')
        if objects: execute(c,'LOCK TABLE public.'+TABLE+' IN '+('ACCESS EXCLUSIVE' if adoption else 'SHARE ROW EXCLUSIVE')+' MODE NOWAIT')
    else:
        driver = getattr(getattr(c,'_connection',c),'driver_connection',None)
        if isinstance(c,Connection): driver = c.connection.driver_connection
        if driver is None: driver = c
        if one(c,'PRAGMA ignore_check_constraints')[0]!=0 or one(c,'PRAGMA foreign_keys')[0]!=1: raise MigrationError('Power enforcement required')
        if not driver.in_transaction: execute(c,'BEGIN IMMEDIATE')
        if one(c,"SELECT 1 FROM sqlite_temp_master WHERE name=? OR name LIKE 'ptop_%'",(TABLE,)): raise MigrationError('Power relation shadowed')
        if one(c,"SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)): execute(c,'UPDATE '+TABLE+' SET revision=revision WHERE 0')


def footprint(c):
    if dialect(c)=='postgresql':
        return bool(one(c,"SELECT 1 FROM pg_class WHERE relname=? OR relname LIKE 'ptop_%' UNION ALL SELECT 1 FROM pg_proc WHERE proname=? UNION ALL SELECT 1 FROM pg_trigger WHERE tgname IN ('power_target_operations_row_guard','power_target_operations_no_truncate')",(TABLE,FUNCTION)))
    return bool(one(c,"SELECT 1 FROM sqlite_master WHERE name=? OR name LIKE 'ptop_%'",(TABLE,)))


def _pg_contract(c, table):
    return {
        'columns':rows(c,"SELECT a.attname,a.atttypid,a.atttypmod,a.attnotnull,pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated,a.attcollation FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE a.attrelid=to_regclass(?) AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",(table,)),
        'dropped':rows(c,'SELECT attnum FROM pg_attribute WHERE attrelid=to_regclass(?) AND attnum>0 AND attisdropped',(table,)),
        'constraints':rows(c,"SELECT conname,contype,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(?) ORDER BY conname",(table,)),
        'indexes':sorted(rows(c,"SELECT ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY x(num,pos) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=x.num ORDER BY pos), i.indisunique,i.indisprimary,i.indisvalid,i.indisready,i.indimmediate,i.indnullsnotdistinct,pg_get_expr(i.indpred,i.indrelid),pg_get_expr(i.indexprs,i.indrelid),i.indnkeyatts,i.indnatts,am.amname,i.indkey::text,i.indclass::text,i.indcollation::text,i.indoption::text,i.indisclustered,i.indisreplident,k.reloptions FROM pg_index i JOIN pg_class k ON k.oid=i.indexrelid JOIN pg_am am ON am.oid=k.relam WHERE i.indrelid=to_regclass(?)",(table,)),key=repr),
        'relation':rows(c,'SELECT relrowsecurity,relforcerowsecurity,relreplident,reloptions,relispartition FROM pg_class WHERE oid=to_regclass(?)',(table,)),
        'inherits':rows(c,'SELECT inhparent FROM pg_inherits WHERE inhrelid=to_regclass(?)',(table,)),
        'policies':rows(c,'SELECT polname FROM pg_policy WHERE polrelid=to_regclass(?)',(table,)),
    }


def _shape(c):
    if dialect(c)=='sqlite':
        stored=one(c,"SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(TABLE,))
        if not stored or tokens(stored[0])!=tokens(ddl(False)): raise MigrationError('Power table shape differs')
        extra = rows(c,"SELECT name FROM sqlite_master WHERE name LIKE 'ptop_%' AND tbl_name<>?",(TABLE,))
        if extra: raise MigrationError('Power reserved object differs')
        expected = indexes(False)
        actual={n:sql for n,sql in rows(c,"SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(TABLE,))}
        if set(actual)!=set(expected) or any(tokens(actual[n])!=tokens(expected[n]) for n in actual): raise MigrationError('Power indices differ')
        # Auto indices must be exactly the primary and nullable global request key.
        auto=[]
        for row in rows(c,'PRAGMA index_list('+TABLE+')'):
            if row[1] not in expected:
                auto.append((row[2],row[3],tuple(x[2] for x in rows(c,'PRAGMA index_info('+row[1]+')'))))
        if sorted(auto)!=[(1,'pk',('operation_nonce',)),(1,'u',('request_nonce',))]: raise MigrationError('Power keys differ')
        expected=sqlite_triggers(); actual=dict(rows(c,"SELECT name,sql FROM sqlite_master WHERE type='trigger' AND (tbl_name=? OR name LIKE 'ptop_%')",(TABLE,)))
        if set(actual)!=set(expected) or any(tokens(actual[n])!=tokens(expected[n]) for n in actual): raise MigrationError('Power triggers differ')
    else:
        if one(c,"SELECT to_regclass('pg_temp._power_target_reference')")[0] is not None: raise MigrationError('Power reference shadowed')
        execute(c,ddl(True,reference=True))
        try:
            for sql in indexes(True,reference=True).values(): execute(c,sql)
            if _pg_contract(c,'public.'+TABLE)!=_pg_contract(c,'pg_temp._power_target_reference'): raise MigrationError('Power physical shape differs')
        finally: execute(c,'DROP TABLE pg_temp._power_target_reference')
        index_names={r[0] for r in rows(c,'SELECT k.relname FROM pg_index i JOIN pg_class k ON k.oid=i.indexrelid WHERE i.indrelid=to_regclass(?)',('public.'+TABLE,))}
        if index_names != {*INDEXES,'ptop_operation_pk','ptop_request_unique'}: raise MigrationError('Power index names differ')
        functions=rows(c,"SELECT p.oid,n.nspname,p.prosrc,p.prosecdef,l.lanname,p.pronargs,p.prorettype::regtype::text,p.proconfig,p.provolatile,p.proisstrict,p.proleakproof,p.proparallel,p.prokind,p.proargdefaults,p.proacl,p.pronargdefaults,p.provariadic,p.proallargtypes,p.proargmodes,p.proargnames,p.prosupport::oid,p.procost,p.prorows FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE p.proname=?",(FUNCTION,))
        if len(functions)!=1: raise MigrationError('Power function union differs')
        oid,namespace,body,*attrs=functions[0]
        wanted=[False,'plpgsql',0,'trigger',['search_path=pg_catalog, public'],'v',False,False,'u','f',None,None,0,0,None,None,None,0,100.0,0.0]
        if namespace!='public' or body!=pg_functions()[FUNCTION] or attrs!=wanted: raise MigrationError('Power function attributes or ordered body differ')
        triggers=rows(c,"SELECT t.tgname,n.nspname,k.relname,t.tgfoid,t.tgtype,t.tgenabled,t.tgisinternal,t.tgconstraint,t.tgdeferrable,t.tginitdeferred,t.tgnargs,t.tgargs,t.tgqual::text,t.tgoldtable,t.tgnewtable FROM pg_trigger t JOIN pg_class k ON k.oid=t.tgrelid JOIN pg_namespace n ON n.oid=k.relnamespace WHERE t.tgrelid=to_regclass(?) OR t.tgname IN ('power_target_operations_row_guard','power_target_operations_no_truncate') ORDER BY t.tgname",('public.'+TABLE,))
        wanted=[(name,'public',TABLE,oid,kind,'O',False,0,False,False,0,b'',None,None,None) for name,kind in [('power_target_operations_no_truncate',34),('power_target_operations_row_guard',31)]]
        if triggers!=wanted: raise MigrationError('Power trigger footprint differs')


def validate(c):
    boundary(c)
    _shape(c)
    validate_retained(c)


def validate_retained(c):
    predicate=' AND '.join('('+value+')' for value in checks(dialect(c)=='postgresql').values())
    # IS NOT TRUE includes NULL/unknown invariants, not just a false WHERE.
    if one(c,'SELECT 1 FROM '+TABLE+' WHERE ('+predicate+') IS NOT TRUE LIMIT 1'): raise MigrationError('Power retained state differs')
    for row in rows(c,'SELECT '+','.join(TIMES)+' FROM '+TABLE):
        for value in row:
            if value is None: continue
            try:
                if dialect(c)=='postgresql':
                    if not isinstance(value,datetime) or value.tzinfo is None: raise ValueError()
                else:
                    parsed=datetime.strptime(value,'%Y-%m-%dT%H:%M:%S.%fZ')
                    if parsed.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]+'Z'!=value: raise ValueError()
            except (ValueError,TypeError,OverflowError): raise MigrationError('Power retained time differs') from None


def apply(c):
    boundary(c,adoption=True)
    if footprint(c):
        validate(c)
        if one(c,'SELECT 1 FROM '+TABLE+' LIMIT 1'): raise MigrationError('Used unrecorded power storage refuses adoption')
        return
    execute(c,ddl(dialect(c)=='postgresql'))
    for sql in indexes(dialect(c)=='postgresql').values(): execute(c,sql)
    if dialect(c)=='postgresql':
        for name,body in pg_functions().items(): execute(c,'CREATE FUNCTION public.'+name+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog, public AS $$'+body+'$$')
        for sql in pg_triggers().values(): execute(c,sql)
    else:
        for sql in sqlite_triggers().values(): execute(c,sql)
    validate(c)
