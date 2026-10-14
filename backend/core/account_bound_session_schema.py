"""Unregistered additive explicit session nonce; no auth or issuer activation."""
from core import account_lifetime_schema as s
TABLE='active_sessions'
FENCES=tuple(sorted((*s.BOUNDARY,'active_sessions','token_blacklist')))


def column(pg=False):
    return 'account_nonce TEXT CHECK(account_nonce IS NULL OR ('+s.nonce('account_nonce',pg)+')) REFERENCES account_lifetimes(account_nonce)'

def _association(prefix='NEW',pg=False,live=True):
    clock="clock_timestamp()" if pg else "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    return f"EXISTS(SELECT 1 FROM account_authorities a JOIN account_lifetimes l ON l.account_nonce=a.account_nonce JOIN account_bindings b ON b.account_nonce=l.account_nonce AND b.user_id=l.current_user_id JOIN users u ON u.id=b.user_id AND u.account_nonce=b.account_nonce WHERE a.jti={prefix}.token_jti AND a.account_nonce={prefix}.account_nonce AND a.purpose='access' AND l.state='active' AND u.id={prefix}.user_id AND u.is_active IS TRUE AND l.born_at<=a.issued_at"+(f' AND a.expires_at>{clock}' if live else '')+')'

def conditions(pg=False):
    same=lambda field:f'NEW.{field} IS NOT DISTINCT FROM OLD.{field}' if pg else f'NEW.{field} IS OLD.{field}'
    return {'insert':"EXISTS(SELECT 1 FROM active_sessions WHERE id=NEW.id OR token_jti=NEW.token_jti) OR (NEW.account_nonce IS NULL AND EXISTS(SELECT 1 FROM account_auth_activation)) OR (NEW.account_nonce IS NOT NULL AND (NOT "+_association(pg=pg)+" OR EXISTS(SELECT 1 FROM token_blacklist WHERE jti=NEW.token_jti)))",'update':'NOT ('+' AND '.join(same(x) for x in ('id','user_id','token_jti','account_nonce'))+')'}

def sqlite_triggers():
    return {'account_session_'+op:f"CREATE TRIGGER account_session_{op} BEFORE {op.upper()} ON active_sessions WHEN {cond} BEGIN SELECT RAISE(ABORT,'Account session binding refused'); END" for op,cond in conditions().items()}

def pg_function():
    body="BEGIN IF TG_OP='TRUNCATE' THEN RAISE EXCEPTION 'Account session binding refused'; END IF; "
    for op,cond in conditions(True).items():body+=f"IF TG_OP='{op.upper()}' THEN IF {cond} THEN RAISE EXCEPTION 'Account session binding refused'; END IF; END IF; "
    body+='RETURN NEW; END';body=s.public_sql(body).replace('FROM active_sessions','FROM public.active_sessions').replace('FROM token_blacklist','FROM public.token_blacklist')
    return body,f'CREATE FUNCTION public.account_session_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $accountsession${body}$accountsession$'

def pg_triggers():
    return {'account_session_lifecycle':'CREATE TRIGGER account_session_lifecycle BEFORE INSERT OR UPDATE ON public.active_sessions FOR EACH ROW EXECUTE FUNCTION public.account_session_guard()','account_session_truncate':'CREATE TRIGGER account_session_truncate BEFORE TRUNCATE ON public.active_sessions FOR EACH STATEMENT EXECUTE FUNCTION public.account_session_guard()'}

def execute(c,sql,args=()):
    # Quote-free source table names only, preserve every literal through core lexical helper.
    if s.dialect(c)=='postgresql':
        sql=s.public_sql(sql)
        import re
        parts=re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|[^A-Za-z_'\"]+",sql)
        output=[]
        for p in parts:
            if p in ('active_sessions','token_blacklist') and (not output or not output[-1].rstrip().endswith('.')):p='public.'+p
            output.append(p)
        sql=''.join(output)
    return s.execute(c,sql,args)

def one(c,sql,args=()):return execute(c,sql,args).fetchone()
def rows(c,sql,args=()):return [tuple(r) for r in execute(c,sql,args).fetchall()]

def relation_oids(c):
    if s.dialect(c)!='postgresql':return None
    result=[]
    for t in ('active_sessions','token_blacklist'):
        if s.one(c,"SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=pg_catalog.pg_my_temp_schema() AND relname=?",(t,))[0]:raise s.AccountLifetimeError('Temporary account session relation refused')
        r=s.one(c,"SELECT r.oid,n.oid,r.relkind,r.relpersistence FROM pg_catalog.pg_class r JOIN pg_catalog.pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' AND r.relname=?",(t,))
        if r is None or r[2:]!=('r','p'):raise s.AccountLifetimeError('Persistent account session relation differs')
        result.append((t,r[0],r[1]))
    return tuple(result)

def install(executor):
    c=s.connection(executor);pg=s.dialect(c)=='postgresql'
    from core.account_bound_session_validation import validate_original,validate
    validate_original(c)
    if pg:
        before=relation_oids(c);s.bounded_timeout(c)
        for table in FENCES:execute(c,'LOCK TABLE '+table+' IN ACCESS EXCLUSIVE MODE NOWAIT')
        if relation_oids(c)!=before:raise s.AccountLifetimeError('Account session relation changed')
    else:execute(c,'UPDATE active_sessions SET id=id WHERE 0')
    execute(c,'ALTER TABLE active_sessions ADD COLUMN '+column(pg))
    if pg:
        execute(c,pg_function()[1])
        for ddl in pg_triggers().values():execute(c,ddl)
    else:
        for ddl in sqlite_triggers().values():execute(c,ddl)
    validate(c)
