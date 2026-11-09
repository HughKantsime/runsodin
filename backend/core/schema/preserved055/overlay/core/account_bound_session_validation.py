"""Exact source shape and retained nonce association; never execute candidate SQL."""
from core import account_lifetime_schema as s
from core import account_bound_session_schema as schema
BASE="CREATE TABLE active_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), token_jti VARCHAR(64) UNIQUE NOT NULL, ip_address VARCHAR(45), user_agent TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP)"

def _refuse(message):raise s.AccountLifetimeError(message)
def _sqlite_shape(c,bound):
    row=s.one(c,"SELECT sql FROM sqlite_schema WHERE type='table' AND name='active_sessions'")
    expected=BASE[:-1]+(', '+schema.column()+')' if bound else ')')
    if row is None or s.tokens(row[0])!=s.tokens(expected):_refuse('Account session table shape differs')
    explicit=s.rows(c,"SELECT name,sql FROM sqlite_schema WHERE type='index' AND tbl_name='active_sessions' AND sql IS NOT NULL")
    if explicit:_refuse('Unexpected account session index')
    if s.one(c,"SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='token_blacklist'")[0]!=1:_refuse('Account session blacklist missing')
    expected_guards=schema.sqlite_triggers() if bound else {}
    actual=dict(s.rows(c,"SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='active_sessions'"))
    if set(actual)!=set(expected_guards) or any(s.tokens(actual[n])!=s.tokens(v) for n,v in expected_guards.items()):_refuse('Account session guard shape differs')

def validate_original(executor):
    c=s.connection(executor)
    if s.dialect(c)=='sqlite':_sqlite_shape(c,False)
    else:
        schema.relation_oids(c)
        actual=s.rows(c,"SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='active_sessions' ORDER BY ordinal_position")
        if tuple(r[0] for r in actual)!=('id','user_id','token_jti','ip_address','user_agent','created_at','last_seen_at'):_refuse('Original account session columns differ')
        # Full source catalog qualification is a prerequisite to PG installer validation.
        if not PG_ORIGINAL:_refuse('Original account session PostgreSQL catalog not qualified')
        _pg_catalog(c,PG_ORIGINAL)

def _pg_catalog(c,expected):
    actual={
      'columns':s.rows(c,"SELECT column_name,data_type,character_maximum_length,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='active_sessions' ORDER BY ordinal_position"),
      'constraints':s.rows(c,"SELECT conname,contype,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid='public.active_sessions'::regclass ORDER BY conname"),
      'indexes':s.rows(c,"SELECT r.relname,pg_get_indexdef(i.indexrelid),i.indisvalid,i.indisready,i.indimmediate FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid WHERE i.indrelid='public.active_sessions'::regclass ORDER BY r.relname")}
    if actual!=expected:_refuse('Account session PostgreSQL catalog differs')

def validate_retained(executor):
    c=s.connection(executor)
    if schema.one(c,"SELECT count(*) FROM active_sessions t WHERE t.account_nonce IS NOT NULL AND NOT "+schema._association('t',s.dialect(c)=='postgresql',False))[0]:_refuse('Account session retained association differs')
    if schema.one(c,"SELECT count(*) FROM active_sessions WHERE account_nonce IS NULL")[0] and s.one(c,'SELECT count(*) FROM account_auth_activation')[0]:_refuse('Legacy session cannot activate')
    if s.dialect(c)=='sqlite' and s.rows(c,'PRAGMA foreign_key_check(active_sessions)'):_refuse('Account session foreign data differs')

def validate(executor):
    c=s.connection(executor)
    if s.dialect(c)=='sqlite':_sqlite_shape(c,True)
    else:
        schema.relation_oids(c)
        if not PG_BOUND:_refuse('Account session PostgreSQL catalog not qualified')
        _pg_catalog(c,PG_BOUND)
        actual=s.rows(c,"SELECT p.prosrc,p.prosecdef,p.proconfig,p.pronargs,p.prorettype='trigger'::regtype,l.lanname,p.procost,p.prorows,p.prosupport::oid,p.pronargdefaults,pg_get_expr(p.proargdefaults,0),p.provolatile,p.proparallel,p.proleakproof,p.proisstrict,p.prokind FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' AND p.proname='account_session_guard'")
        expected=(schema.pg_function()[0],False,['search_path=pg_catalog, public'],0,True,'plpgsql',100.0,0.0,0,0,None,'v','u',False,False,'f')
        if actual!=[expected]:_refuse('Account session function shape differs')
        guards=s.rows(c,"SELECT t.tgname,pg_get_triggerdef(t.oid),t.tgenabled,p.proname,n.nspname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace WHERE NOT t.tgisinternal AND t.tgrelid='public.active_sessions'::regclass ORDER BY t.tgname")
        if guards!=PG_TRIGGERS:_refuse('Account session PostgreSQL guard shape differs')
    validate_retained(c)

# Exact metadata from reviewed source DDL in owned PG diagnostic52910; no candidate adoption.
PG_ORIGINAL = {'columns': [('id', 'bigint', None, 'NO', "nextval('active_sessions_id_seq'::regclass)"),
             ('user_id', 'integer', None, 'NO', None),
             ('token_jti', 'character varying', 64, 'NO', None),
             ('ip_address', 'character varying', 45, 'YES', None),
             ('user_agent', 'text', None, 'YES', None),
             ('created_at', 'timestamp with time zone', None, 'YES', 'CURRENT_TIMESTAMP'),
             ('last_seen_at', 'timestamp with time zone', None, 'YES', 'CURRENT_TIMESTAMP')],
 'constraints': [('active_sessions_pkey', 'p', 'PRIMARY KEY (id)', True, False, False),
                 ('active_sessions_token_jti_key', 'u', 'UNIQUE (token_jti)', True, False, False),
                 ('active_sessions_user_id_fkey',
                  'f',
                  'FOREIGN KEY (user_id) REFERENCES users(id)',
                  True,
                  False,
                  False)],
 'indexes': [('active_sessions_pkey',
              'CREATE UNIQUE INDEX active_sessions_pkey ON public.active_sessions USING btree (id)',
              True,
              True,
              True),
             ('active_sessions_token_jti_key',
              'CREATE UNIQUE INDEX active_sessions_token_jti_key ON public.active_sessions USING btree (token_jti)',
              True,
              True,
              True)]}
PG_BOUND = {'columns': [('id', 'bigint', None, 'NO', "nextval('active_sessions_id_seq'::regclass)"),
             ('user_id', 'integer', None, 'NO', None),
             ('token_jti', 'character varying', 64, 'NO', None),
             ('ip_address', 'character varying', 45, 'YES', None),
             ('user_agent', 'text', None, 'YES', None),
             ('created_at', 'timestamp with time zone', None, 'YES', 'CURRENT_TIMESTAMP'),
             ('last_seen_at', 'timestamp with time zone', None, 'YES', 'CURRENT_TIMESTAMP'),
             ('account_nonce', 'text', None, 'YES', None)],
 'constraints': [('active_sessions_account_nonce_check',
                  'c',
                  'CHECK (((account_nonce IS NULL) OR ((octet_length(account_nonce) = 32) AND ((account_nonce COLLATE '
                  '"C") ~ \'^[0-9a-f]{32}$\'::text))))',
                  True,
                  False,
                  False),
                 ('active_sessions_account_nonce_fkey',
                  'f',
                  'FOREIGN KEY (account_nonce) REFERENCES account_lifetimes(account_nonce)',
                  True,
                  False,
                  False),
                 ('active_sessions_pkey', 'p', 'PRIMARY KEY (id)', True, False, False),
                 ('active_sessions_token_jti_key', 'u', 'UNIQUE (token_jti)', True, False, False),
                 ('active_sessions_user_id_fkey',
                  'f',
                  'FOREIGN KEY (user_id) REFERENCES users(id)',
                  True,
                  False,
                  False)],
 'indexes': [('active_sessions_pkey',
              'CREATE UNIQUE INDEX active_sessions_pkey ON public.active_sessions USING btree (id)',
              True,
              True,
              True),
             ('active_sessions_token_jti_key',
              'CREATE UNIQUE INDEX active_sessions_token_jti_key ON public.active_sessions USING btree (token_jti)',
              True,
              True,
              True)]}
PG_TRIGGERS = [('account_session_lifecycle',
  'CREATE TRIGGER account_session_lifecycle BEFORE INSERT OR UPDATE ON public.active_sessions FOR EACH ROW EXECUTE '
  'FUNCTION account_session_guard()',
  'O',
  'account_session_guard',
  'public'),
 ('account_session_truncate',
  'CREATE TRIGGER account_session_truncate BEFORE TRUNCATE ON public.active_sessions FOR EACH STATEMENT EXECUTE '
  'FUNCTION account_session_guard()',
  'O',
  'account_session_guard',
  'public')]
