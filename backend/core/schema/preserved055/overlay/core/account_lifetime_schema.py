"""Unregistered account storage foundation. No authentication or enrollment activation."""
import re
import sqlite3
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

class AccountLifetimeError(RuntimeError): pass
TABLES=('account_lifetimes','account_bindings','account_authorities','account_retirement_receipts','account_auth_activation')
BOUNDARY=tuple(sorted((*TABLES,'users')))
PURPOSES=('access','mfa_login','mfa_enrollment','websocket','oidc_pending')

def physical(executor):
    if isinstance(executor,Connection):return executor.connection.driver_connection
    if type(executor) is sqlite3.Connection:return executor
    import sys
    module=sys.modules.get('core.db_utils')
    adapter=getattr(module,'_ConnectionAdapter',None)
    if adapter is None or type(executor) is not adapter:raise AccountLifetimeError('Unsupported account executor')
    from sqlalchemy.pool.base import _ConnectionFairy
    raw=executor._connection
    if isinstance(raw,_ConnectionFairy):return raw.driver_connection
    # Actual adapters can also own an actual psycopg driver, never an info duck type.
    try:
        import psycopg
    except ImportError:raise AccountLifetimeError('Unsupported account driver') from None
    if type(raw) is not psycopg.Connection:raise AccountLifetimeError('Unsupported account driver')
    return raw

def connection(executor):
    if isinstance(executor,Session):
        if not executor.is_active or not executor.in_transaction() or executor.in_nested_transaction():raise AccountLifetimeError('Physical root caller transaction required')
        executor=executor.connection()
    if isinstance(executor,Connection):
        if executor.closed or executor.invalidated or not executor.in_transaction() or executor.in_nested_transaction():raise AccountLifetimeError('Physical root caller transaction required')
    driver=physical(executor)
    if type(driver) is sqlite3.Connection:
        try:
            if not driver.in_transaction:raise AccountLifetimeError('Physical caller transaction required')
            if driver.execute('PRAGMA foreign_keys').fetchone()[0]!=1:raise AccountLifetimeError('Foreign-key enforcement required')
            if driver.execute('PRAGMA read_uncommitted').fetchone()[0]!=0:raise AccountLifetimeError('Account transaction isolation differs')
        except sqlite3.ProgrammingError:raise AccountLifetimeError('Closed account driver') from None
    else:
        try:
            import psycopg
        except ImportError:raise AccountLifetimeError('Unsupported account driver') from None
        if type(driver) is not psycopg.Connection:raise AccountLifetimeError('Unsupported account driver')
        if driver.closed or driver.broken or driver.autocommit or int(driver.info.transaction_status)!=2:raise AccountLifetimeError('Physical root caller transaction required')
        if driver.execute('SHOW transaction_isolation').fetchone()[0]!='read committed':raise AccountLifetimeError('Account transaction isolation differs')
        if not isinstance(executor,Connection) and not executor._postgres:raise AccountLifetimeError('Account adapter dialect differs')
    return executor

def dialect(executor):
    c=connection(executor)
    if isinstance(c,Connection):name=c.dialect.name
    else:name='sqlite' if type(physical(c)) is sqlite3.Connection else 'postgresql'
    if name not in ('sqlite','postgresql'):raise AccountLifetimeError('Unsupported account dialect')
    return name

def public_sql(sql):
    # Lexical qualification touches source-owned identifiers, never literals/quoted strings.
    parts=re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|[^A-Za-z_'\"]+",sql)
    output=[]
    for part in parts:
        if part in BOUNDARY and (not output or not output[-1].rstrip().endswith('.')):part='public.'+part
        output.append(part)
    return ''.join(output)

def persistent_oids(executor,*,full=True):
    if dialect(executor)!='postgresql':return None
    result=[]
    for table in (BOUNDARY if full else ('users',)):
        if one(executor,"SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=pg_catalog.pg_my_temp_schema() AND relname=?",(table,))[0]:raise AccountLifetimeError('Temporary account relation refused')
        value=one(executor,"SELECT c.oid,c.relkind,c.relpersistence,n.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=?",(table,))
        if value is None or value[1]!='r' or value[2]!='p':raise AccountLifetimeError('Persistent account relation differs')
        result.append((table,value[0],value[3]))
    return tuple(result)


def execute(executor,sql,values=()):
    c=connection(executor)
    if dialect(c)=='postgresql':
        # Fixed source-owned templates only; protect percent literals before placeholders.
        sql=public_sql(sql).replace('%','%%').replace('?','%s')
    if isinstance(c,Connection):return c.exec_driver_sql(sql,tuple(values))
    return c.execute(sql,tuple(values))
def rows(c,sql,args=()):return [tuple(r) for r in execute(c,sql,args).fetchall()]
def one(c,sql,args=()):return execute(c,sql,args).fetchone()
def tokens(sql):return re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]",sql.strip().rstrip(';'))

def nonce(x,pg=False,width=32):
    return f"octet_length({x})={width} AND {x} COLLATE \"C\" ~ '^[0-9a-f]{{{width}}}$'" if pg else f"typeof({x})='text' AND length({x})={width} AND {x} NOT GLOB '*[^0-9a-f]*'"
def identifier(x,pg=False):return f'{x} BETWEEN 1 AND 2147483647'+('' if pg else f" AND typeof({x})='integer'")
def utc(x,pg=False):
    if pg:return f"isfinite({x}) AND EXTRACT(YEAR FROM {x} AT TIME ZONE 'UTC') BETWEEN 1 AND 9999 AND date_trunc('milliseconds',{x})={x}"
    shape='[0-9]'*4+'-'+('[0-9]'*2)+'-'+('[0-9]'*2)+'T'+('[0-9]'*2)+':'+('[0-9]'*2)+':'+('[0-9]'*2)+'.'+('[0-9]'*3)+'Z'
    return f"typeof({x})='text' AND length({x})=24 AND {x} GLOB '{shape}' AND substr({x},1,4) BETWEEN '0001' AND '9999' AND substr({x},12,2) BETWEEN '00' AND '23' AND substr({x},15,2) BETWEEN '00' AND '59' AND substr({x},18,2) BETWEEN '00' AND '59' AND strftime('%Y-%m-%dT%H:%M:%fZ',{x},'+0 days') IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ',{x},'+0 days')={x}"
def check(name,expr):return f'CONSTRAINT {name} CHECK({expr})'

def tables(pg=False):
    time='TIMESTAMPTZ' if pg else 'TEXT';idtype='INTEGER';n=lambda x:nonce(x,pg);i=lambda x:identifier(x,pg);t=lambda x:utc(x,pg)
    deferred='DEFERRABLE INITIALLY DEFERRED'
    declarations={
      'account_lifetimes':f"account_nonce TEXT NOT NULL PRIMARY KEY, birth_user_id {idtype} NOT NULL, current_user_id {idtype} UNIQUE, state TEXT NOT NULL, born_at {time} NOT NULL, retired_at {time}, UNIQUE(account_nonce,current_user_id), FOREIGN KEY(current_user_id,account_nonce) REFERENCES users(id,account_nonce) {deferred}, "+', '.join([check('acl_nonce',n('account_nonce')),check('acl_birth_id',i('birth_user_id')),check('acl_current_id','current_user_id IS NULL OR ('+i('current_user_id')+')'),check('acl_birth_time',t('born_at')),check('acl_retire_time','retired_at IS NULL OR ('+t('retired_at')+')'),check('acl_state',"(state='active' AND current_user_id IS NOT NULL AND current_user_id=birth_user_id AND retired_at IS NULL) OR (state='retired' AND current_user_id IS NULL AND retired_at IS NOT NULL AND retired_at>=born_at)")]),
      'account_bindings':f"account_nonce TEXT NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, FOREIGN KEY(account_nonce,user_id) REFERENCES account_lifetimes(account_nonce,current_user_id) {deferred}, "+check('acb_nonce',n('account_nonce'))+', '+check('acb_user',i('user_id')),
      'account_authorities':f"jti TEXT NOT NULL PRIMARY KEY, account_nonce TEXT NOT NULL REFERENCES account_lifetimes(account_nonce), purpose TEXT NOT NULL, issued_at {time} NOT NULL, expires_at {time} NOT NULL, "+', '.join([check('aca_jti',"octet_length(jti) BETWEEN 1 AND 128 AND jti COLLATE \"C\" ~ '^[!-~]+$'" if pg else "typeof(jti)='text' AND length(jti) BETWEEN 1 AND 128 AND jti NOT GLOB '*[^!-~]*'"),check('aca_nonce',n('account_nonce')),check('aca_purpose','purpose IN ('+','.join("'"+x+"'" for x in PURPOSES)+')'),check('aca_issued',t('issued_at')),check('aca_expires',t('expires_at')),check('aca_order','expires_at>issued_at')]),
      'account_retirement_receipts':f"operation_nonce TEXT NOT NULL PRIMARY KEY, account_nonce TEXT NOT NULL UNIQUE REFERENCES account_lifetimes(account_nonce), actor_account_nonce TEXT NOT NULL REFERENCES account_lifetimes(account_nonce), request_sha256 TEXT NOT NULL, retired_at {time} NOT NULL, result TEXT NOT NULL, "+', '.join([check('acr_operation',n('operation_nonce')),check('acr_target',n('account_nonce')),check('acr_actor',n('actor_account_nonce')),check('acr_digest',nonce('request_sha256',pg,64)),check('acr_time',t('retired_at')),check('acr_result',"result='retired'")]),
      'account_auth_activation':f"singleton INTEGER NOT NULL PRIMARY KEY, version INTEGER NOT NULL, activated_at {time} NOT NULL, "+check('acv_singleton','singleton=1')+', '+check('acv_version','version=2')+', '+check('acv_time',t('activated_at'))}
    return {k:f'CREATE TABLE {k} ({v})' for k,v in declarations.items()}

def user_column(pg=False):return 'account_nonce TEXT CHECK(account_nonce IS NULL OR ('+nonce('account_nonce',pg)+')) REFERENCES account_bindings(account_nonce) DEFERRABLE INITIALLY DEFERRED'
INDEXES={'uq_users_account_nonce':'CREATE UNIQUE INDEX uq_users_account_nonce ON users(account_nonce)','uq_users_id_account_nonce':'CREATE UNIQUE INDEX uq_users_id_account_nonce ON users(id,account_nonce)'}
def active(n,idexpr=None):
    result=f"EXISTS(SELECT 1 FROM account_lifetimes l JOIN account_bindings b ON b.account_nonce=l.account_nonce AND b.user_id=l.current_user_id JOIN users u ON u.id=b.user_id AND u.account_nonce=b.account_nonce WHERE l.account_nonce={n} AND l.state='active'"
    return result+(f' AND l.current_user_id={idexpr}' if idexpr else '')+')'

def conditions(pg=False):
    same=lambda c:f'NEW.{c} IS NOT DISTINCT FROM OLD.{c}' if pg else f'NEW.{c} IS OLD.{c}'
    result={
      'users':{'insert':"EXISTS(SELECT 1 FROM users WHERE id=NEW.id OR username=NEW.username OR (NEW.account_nonce IS NOT NULL AND account_nonce=NEW.account_nonce) OR (NEW.oidc_issuer IS NOT NULL AND NEW.oidc_subject IS NOT NULL AND oidc_issuer=NEW.oidc_issuer AND oidc_subject=NEW.oidc_subject))",'update':f"(OLD.account_nonce IS NOT NULL AND NOT ({same('id')} AND {same('account_nonce')})) OR (OLD.account_nonce IS NULL AND NEW.account_nonce IS NOT NULL)"},
      'account_lifetimes':{'insert':"NEW.state<>'active' OR NEW.current_user_id IS NULL OR NEW.current_user_id<>NEW.birth_user_id OR NEW.retired_at IS NOT NULL OR EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=NEW.account_nonce OR current_user_id=NEW.current_user_id)",'update':"NOT (OLD.state='active' AND NEW.state='retired' AND NEW.current_user_id IS NULL AND NEW.retired_at IS NOT NULL AND "+' AND '.join(same(c) for c in ('account_nonce','birth_user_id','born_at'))+" AND NOT EXISTS(SELECT 1 FROM account_authorities WHERE account_nonce=OLD.account_nonce AND issued_at>NEW.retired_at) AND NOT EXISTS(SELECT 1 FROM account_retirement_receipts WHERE actor_account_nonce=OLD.account_nonce AND retired_at>NEW.retired_at))",'delete':'1=1'},
      'account_bindings':{'insert':"EXISTS(SELECT 1 FROM account_bindings WHERE account_nonce=NEW.account_nonce OR user_id=NEW.user_id) OR NOT EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=NEW.account_nonce AND current_user_id=NEW.user_id AND birth_user_id=NEW.user_id AND state='active')",'update':'1=1','delete':"NOT EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=OLD.account_nonce AND birth_user_id=OLD.user_id AND current_user_id IS NULL AND state='retired')"},
      'account_authorities':{'insert':"EXISTS(SELECT 1 FROM account_authorities WHERE jti=NEW.jti) OR NOT ("+active('NEW.account_nonce')+" AND EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=NEW.account_nonce AND born_at<=NEW.issued_at))",'update':'1=1','delete':'1=1'},
      'account_retirement_receipts':{'insert':"EXISTS(SELECT 1 FROM account_retirement_receipts WHERE operation_nonce=NEW.operation_nonce OR account_nonce=NEW.account_nonce) OR NOT EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=NEW.account_nonce AND state='retired' AND retired_at=NEW.retired_at) OR NOT (("+active('NEW.actor_account_nonce')+" AND EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=NEW.actor_account_nonce AND born_at<=NEW.retired_at)) OR (NEW.actor_account_nonce=NEW.account_nonce AND EXISTS(SELECT 1 FROM account_lifetimes WHERE account_nonce=NEW.actor_account_nonce AND state='retired' AND current_user_id IS NULL AND retired_at=NEW.retired_at)))",'update':'1=1','delete':'1=1'},
      'account_auth_activation':{'insert':'1=1','update':'1=1','delete':'1=1'}}
    return result

def trigger_name(table,op):return 'account_028_'+table.removeprefix('account_')+'_'+op

def sqlite_triggers():
    return {trigger_name(table,op):f"CREATE TRIGGER {trigger_name(table,op)} BEFORE {op.upper()} ON {table} WHEN {cond} BEGIN SELECT RAISE(ABORT,'Account lifetime guard refused'); END" for table,ops in conditions().items() for op,cond in ops.items()}

def pg_functions():
    result={}
    for table,ops in conditions(True).items():
        body="BEGIN IF TG_OP='TRUNCATE' THEN RAISE EXCEPTION 'Account lifetime guard refused'; END IF; "
        for op,cond in ops.items():body+=f"IF TG_OP='{op.upper()}' THEN IF {cond} THEN RAISE EXCEPTION 'Account lifetime guard refused'; END IF; END IF; "
        body+="IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW; END"
        name='account_028_'+table.removeprefix('account_')+'_guard'
        body=public_sql(body)
        result[name]=(body,f"CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $account028${body}$account028$")
    return result

def pg_triggers():
    result={}
    for table,ops in conditions(True).items():
        function='account_028_'+table.removeprefix('account_')+'_guard'
        for suffix,events in [('lifecycle',' OR '.join(o.upper() for o in ops)),('truncate','TRUNCATE')]:
            name=trigger_name(table,suffix)
            result[name]=f'CREATE TRIGGER {name} BEFORE {events} ON {table} FOR EACH '+('STATEMENT' if suffix=='truncate' else 'ROW')+f' EXECUTE FUNCTION public.{function}()'
    return result

def bounded_timeout(executor):
    raw=one(executor,'SHOW lock_timeout')[0]
    match=re.fullmatch(r'([0-9]+)(ms|s|min|h)?',raw)
    if match is None:raise AccountLifetimeError('Unsupported account lock timeout')
    value=int(match[1])*{'ms':1,'s':1000,'min':60000,'h':3600000,None:1}[match[2]]
    if value==0 or value>2000:execute(executor,"SET LOCAL lock_timeout='2000ms'")


def install(executor):
    c=connection(executor);pg=dialect(c)=='postgresql'
    if pg:persistent_oids(c,full=False)
    existing=rows(c,"SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'account_%'" if pg else "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'account_%'")
    usercols=rows(c,"SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='users'" if pg else 'PRAGMA table_xinfo(users)')
    names={r[0] if pg else r[1] for r in usercols}
    if not {'id','username','oidc_issuer','oidc_subject'}<=names or existing or 'account_nonce' in names:raise AccountLifetimeError('Fresh account foundation footprint required')
    if not pg:
        unique=rows(c,'PRAGMA index_list(users)')
        for row in unique:
            if row[2] and tuple(r[2] for r in rows(c,'PRAGMA index_info("'+row[1]+'")')) not in (('username',),('id','group_id'),('oidc_issuer','oidc_subject')):raise AccountLifetimeError('Unsupported original users unique key')
    if pg:
        bounded_timeout(c)
        expected_users=persistent_oids(c,full=False)
        execute(c,'LOCK TABLE public.users IN ACCESS EXCLUSIVE MODE NOWAIT')
        if persistent_oids(c,full=False)!=expected_users:raise AccountLifetimeError('Persistent account relation changed')
    # The circular referenced UNIQUE exists before SQLite creates the referencing tables.
    # PostgreSQL installs lifetimes' reverse FK after both keys are present.
    definitions=tables(pg)
    reverse='FOREIGN KEY(current_user_id,account_nonce) REFERENCES users(id,account_nonce) DEFERRABLE INITIALLY DEFERRED, '
    for table,ddl in definitions.items():execute(c,ddl.replace(reverse,'') if pg and table=='account_lifetimes' else ddl)
    execute(c,'ALTER TABLE users ADD COLUMN '+user_column(pg))
    for ddl in INDEXES.values():execute(c,ddl)
    if pg:
        execute(c,'ALTER TABLE account_lifetimes ADD CONSTRAINT fk_account_lifetimes_users '+reverse.rstrip(', '))
        for _,ddl in pg_functions().values():execute(c,ddl)
        for ddl in pg_triggers().values():execute(c,ddl)
    else:
        for ddl in sqlite_triggers().values():execute(c,ddl)
    from core.account_lifetime_validation import validate
    validate(c)
