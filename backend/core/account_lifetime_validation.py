"""Source-owned shape and retained account-cycle validation; never evaluates candidate SQL."""
from datetime import datetime,timezone
import re
from core.account_lifetime_schema import (AccountLifetimeError,TABLES,INDEXES,connection,dialect,rows,one,tables,tokens,user_column,sqlite_triggers,pg_functions,pg_triggers,PURPOSES)

def _refuse(message):raise AccountLifetimeError(message)
def _time(value):
    if isinstance(value,datetime):
        if value.tzinfo is None or value.microsecond%1000:_refuse('Invalid retained account timestamp')
        return value.astimezone(timezone.utc)
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z',value):_refuse('Invalid retained account timestamp')
    try:return datetime.strptime(value,'%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    except ValueError:_refuse('Invalid retained account timestamp')
def _nonce(value,width=32):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{'+str(width)+'}',value):_refuse('Invalid retained account nonce')
def _id(value):
    if type(value) is not int or not 1<=value<=2147483647:_refuse('Invalid retained account identifier')

def validate_retained(executor):
    c=connection(executor)
    lifetimes={}
    for n,b,current,state,born,retired in rows(c,'SELECT account_nonce,birth_user_id,current_user_id,state,born_at,retired_at FROM account_lifetimes'):
        _nonce(n);_id(b);birth=_time(born)
        if state=='active':
            if current!=b or retired is not None:_refuse('Invalid active account lifetime')
        elif state=='retired':
            if current is not None or retired is None or _time(retired)<birth:_refuse('Invalid retired account lifetime')
        else:_refuse('Invalid account lifetime state')
        lifetimes[n]=(b,current,state,born,retired)
    bindings={}
    for n,uid in rows(c,'SELECT account_nonce,user_id FROM account_bindings'):
        _nonce(n);_id(uid)
        if n in bindings or uid in bindings.values() or lifetimes.get(n,())[:3]!=(uid,uid,'active'):_refuse('Account live relay differs')
        bindings[n]=uid
    userbindings={}
    for uid,n in rows(c,'SELECT id,account_nonce FROM users WHERE account_nonce IS NOT NULL'):
        _nonce(n);_id(uid)
        if n in userbindings or bindings.get(n)!=uid:_refuse('Account user binding differs')
        userbindings[n]=uid
    for n,(_,current,state,_,_) in lifetimes.items():
        if state=='active' and (bindings.get(n)!=current or userbindings.get(n)!=current):_refuse('Account active cycle missing')
        if state=='retired' and (n in bindings or n in userbindings):_refuse('Retired account rebound')
    for jti,n,purpose,issued,expires in rows(c,'SELECT jti,account_nonce,purpose,issued_at,expires_at FROM account_authorities'):
        _nonce(n)
        if type(jti) is not str or not re.fullmatch('[!-~]{1,128}',jti) or purpose not in PURPOSES or n not in lifetimes or _time(expires)<=_time(issued):_refuse('Account authority differs')
        if _time(issued)<_time(lifetimes[n][3]):_refuse('Authority predates account birth')
        if lifetimes[n][2]=='retired' and _time(issued)>_time(lifetimes[n][4]):_refuse('Authority postdates retirement')
    for op,n,actor,digest,retired,result in rows(c,'SELECT operation_nonce,account_nonce,actor_account_nonce,request_sha256,retired_at,result FROM account_retirement_receipts'):
        _nonce(op);_nonce(n);_nonce(actor);_nonce(digest,64)
        if n not in lifetimes or actor not in lifetimes or lifetimes[n][2]!='retired' or _time(retired)!=_time(lifetimes[n][4]) or result!='retired':_refuse('Account retirement receipt differs')
        if _time(retired)<_time(lifetimes[actor][3]) or (lifetimes[actor][2]=='retired' and _time(retired)>_time(lifetimes[actor][4])):_refuse('Receipt actor lifetime differs')
    if one(c,'SELECT count(*) FROM account_auth_activation')[0]:_refuse('Account authentication activation unsupported')
    if dialect(c)=='sqlite' and rows(c,'PRAGMA foreign_key_check'):_refuse('Account foreign data differs')

def validate(executor):
    c=connection(executor);pg=dialect(c)=='postgresql'
    if not pg:
        for table,ddl in tables().items():
            actual=one(c,"SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",(table,))
            if actual is None or tokens(actual[0])!=tokens(ddl):_refuse('Account table shape differs')
        actual=one(c,"SELECT sql FROM sqlite_schema WHERE type='table' AND name='users'")
        if actual is None or tokens(user_column())!=tokens(actual[0][actual[0].rfind('account_nonce TEXT'):].rstrip().removesuffix(')')):_refuse('Account users column differs')
        for name,ddl in INDEXES.items():
            row=one(c,"SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",(name,))
            if row is None or tokens(row[0])!=tokens(ddl):_refuse('Account users unique shape differs')
        actual=dict(rows(c,"SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name GLOB 'account_028_*'"))
        expected=sqlite_triggers()
        if set(actual)!=set(expected) or any(tokens(actual[n])!=tokens(ddl) for n,ddl in expected.items()):_refuse('Account guard shape differs')
        # No additional account indexes/guards can be accepted as harmless narrowing.
        for table in TABLES:
            if rows(c,"SELECT name FROM sqlite_schema WHERE tbl_name=? AND type='index' AND sql IS NOT NULL",(table,)):_refuse('Unexpected account index')
            if any(r[0] not in expected for r in rows(c,"SELECT name FROM sqlite_schema WHERE tbl_name=? AND type='trigger'",(table,))):_refuse('Unexpected account guard')
    else:
        from core.account_lifetime_schema import BOUNDARY,persistent_oids
        persistent_oids(c)
        for table in (*TABLES,'users'):
            actual_columns=rows(c,"SELECT column_name,data_type,is_nullable,column_default,collation_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",(table,))
            if actual_columns!=[tuple(x[k] for k in ('column_name','data_type','is_nullable','column_default','collation_name')) for x in PG_CATALOG['columns'][table]]:_refuse('Account PostgreSQL column shape differs')
            actual_constraints=rows(c,"SELECT conname,contype,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred,pg_get_expr(conbin,conrelid) FROM pg_constraint WHERE conrelid=to_regclass(?) ORDER BY conname",('public.'+table,))
            expected_constraints=[tuple(x[k] for k in ('conname','contype','definition','convalidated','condeferrable','condeferred','expression')) for x in PG_CATALOG['constraints'][table]]
            if actual_constraints!=expected_constraints:_refuse('Account PostgreSQL constraint shape differs')
            actual_indexes=rows(c,"SELECT r.relname,pg_get_indexdef(i.indexrelid),i.indisvalid,i.indisready,i.indimmediate FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid WHERE i.indrelid=to_regclass(?) ORDER BY r.relname",('public.'+table,))
            if actual_indexes!=[tuple(x[k] for k in ('indexname','indexdef','indisvalid','indisready','indimmediate')) for x in PG_CATALOG['indexes'][table]]:_refuse('Account PostgreSQL index shape differs')
            actual_references=rows(c,"SELECT x.conname,n.nspname,r.relname,x.confkey,x.conkey FROM pg_constraint x JOIN pg_class r ON r.oid=x.confrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE x.contype='f' AND x.conrelid=to_regclass(?) ORDER BY x.conname",('public.'+table,))
            if actual_references!=[tuple(x[k] for k in ('conname','target_schema','target_table','confkey','conkey')) for x in PG_CATALOG['oid_references'][table]]:_refuse('Account PostgreSQL FK identity differs')
            actual_triggers=rows(c,"SELECT t.tgname,pg_get_triggerdef(t.oid),t.tgenabled,t.tgtype,p.proname,n.nspname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace WHERE NOT t.tgisinternal AND t.tgrelid=to_regclass(?)"+(" AND t.tgname LIKE 'account_028_%'" if table=='users' else '')+" ORDER BY t.tgname",('public.'+table,))
            if actual_triggers!=[tuple(x[k] for k in ('tgname','definition','tgenabled','tgtype','proname','function_schema')) for x in PG_CATALOG['triggers'][table]]:_refuse('Account PostgreSQL trigger shape differs')
        functions=pg_functions()
        actual=rows(c,"SELECT p.proname,p.prosrc,p.prosecdef,p.proconfig,p.pronargs,p.prorettype='trigger'::regtype,p.provolatile,p.proparallel,l.lanname,p.proleakproof,p.proisstrict,p.prokind,p.procost,p.prorows,p.prosupport::oid,p.pronargdefaults,pg_get_expr(p.proargdefaults,0) FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname LIKE 'account_028_%'")
        if len(actual)!=len(functions):_refuse('Account function union differs')
        for name,body,security,config,args,rettype,volatile,parallel,language,leakproof,strict,kind,cost,result_rows,support,default_count,defaults in actual:
            if name not in functions or body!=functions[name][0] or security or config!=['search_path=pg_catalog, public'] or args or not rettype or volatile!='v' or parallel!='u' or language!='plpgsql' or leakproof or strict or kind!='f' or cost!=100 or result_rows!=0 or support!=0 or default_count!=0 or defaults is not None:_refuse('Account function shape differs')
    validate_retained(c)

# Exact catalog emitted ONLY by source-authored reviewed DDL in owned PostgreSQL.
# Diagnostic receipt account-lifetime-catalog-native.json (24481); no live candidate adoption.
PG_CATALOG = {'columns': {'account_auth_activation': [{'collation_name': None,
                                          'column_default': None,
                                          'column_name': 'singleton',
                                          'data_type': 'integer',
                                          'is_nullable': 'NO'},
                                         {'collation_name': None,
                                          'column_default': None,
                                          'column_name': 'version',
                                          'data_type': 'integer',
                                          'is_nullable': 'NO'},
                                         {'collation_name': None,
                                          'column_default': None,
                                          'column_name': 'activated_at',
                                          'data_type': 'timestamp with time zone',
                                          'is_nullable': 'NO'}],
             'account_authorities': [{'collation_name': None,
                                      'column_default': None,
                                      'column_name': 'jti',
                                      'data_type': 'text',
                                      'is_nullable': 'NO'},
                                     {'collation_name': None,
                                      'column_default': None,
                                      'column_name': 'account_nonce',
                                      'data_type': 'text',
                                      'is_nullable': 'NO'},
                                     {'collation_name': None,
                                      'column_default': None,
                                      'column_name': 'purpose',
                                      'data_type': 'text',
                                      'is_nullable': 'NO'},
                                     {'collation_name': None,
                                      'column_default': None,
                                      'column_name': 'issued_at',
                                      'data_type': 'timestamp with time zone',
                                      'is_nullable': 'NO'},
                                     {'collation_name': None,
                                      'column_default': None,
                                      'column_name': 'expires_at',
                                      'data_type': 'timestamp with time zone',
                                      'is_nullable': 'NO'}],
             'account_bindings': [{'collation_name': None,
                                   'column_default': None,
                                   'column_name': 'account_nonce',
                                   'data_type': 'text',
                                   'is_nullable': 'NO'},
                                  {'collation_name': None,
                                   'column_default': None,
                                   'column_name': 'user_id',
                                   'data_type': 'integer',
                                   'is_nullable': 'NO'}],
             'account_lifetimes': [{'collation_name': None,
                                    'column_default': None,
                                    'column_name': 'account_nonce',
                                    'data_type': 'text',
                                    'is_nullable': 'NO'},
                                   {'collation_name': None,
                                    'column_default': None,
                                    'column_name': 'birth_user_id',
                                    'data_type': 'integer',
                                    'is_nullable': 'NO'},
                                   {'collation_name': None,
                                    'column_default': None,
                                    'column_name': 'current_user_id',
                                    'data_type': 'integer',
                                    'is_nullable': 'YES'},
                                   {'collation_name': None,
                                    'column_default': None,
                                    'column_name': 'state',
                                    'data_type': 'text',
                                    'is_nullable': 'NO'},
                                   {'collation_name': None,
                                    'column_default': None,
                                    'column_name': 'born_at',
                                    'data_type': 'timestamp with time zone',
                                    'is_nullable': 'NO'},
                                   {'collation_name': None,
                                    'column_default': None,
                                    'column_name': 'retired_at',
                                    'data_type': 'timestamp with time zone',
                                    'is_nullable': 'YES'}],
             'account_retirement_receipts': [{'collation_name': None,
                                              'column_default': None,
                                              'column_name': 'operation_nonce',
                                              'data_type': 'text',
                                              'is_nullable': 'NO'},
                                             {'collation_name': None,
                                              'column_default': None,
                                              'column_name': 'account_nonce',
                                              'data_type': 'text',
                                              'is_nullable': 'NO'},
                                             {'collation_name': None,
                                              'column_default': None,
                                              'column_name': 'actor_account_nonce',
                                              'data_type': 'text',
                                              'is_nullable': 'NO'},
                                             {'collation_name': None,
                                              'column_default': None,
                                              'column_name': 'request_sha256',
                                              'data_type': 'text',
                                              'is_nullable': 'NO'},
                                             {'collation_name': None,
                                              'column_default': None,
                                              'column_name': 'retired_at',
                                              'data_type': 'timestamp with time zone',
                                              'is_nullable': 'NO'},
                                             {'collation_name': None,
                                              'column_default': None,
                                              'column_name': 'result',
                                              'data_type': 'text',
                                              'is_nullable': 'NO'}],
             'users': [{'collation_name': None,
                        'column_default': "nextval('users_id_seq'::regclass)",
                        'column_name': 'id',
                        'data_type': 'bigint',
                        'is_nullable': 'NO'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'username',
                        'data_type': 'character varying',
                        'is_nullable': 'NO'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'email',
                        'data_type': 'character varying',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'password_hash',
                        'data_type': 'character varying',
                        'is_nullable': 'NO'},
                       {'collation_name': None,
                        'column_default': "'viewer'::character varying",
                        'column_name': 'role',
                        'data_type': 'character varying',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': 'true',
                        'column_name': 'is_active',
                        'data_type': 'boolean',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'last_login',
                        'data_type': 'timestamp with time zone',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': 'CURRENT_TIMESTAMP',
                        'column_name': 'created_at',
                        'data_type': 'timestamp with time zone',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'oidc_subject',
                        'data_type': 'character varying',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'oidc_provider',
                        'data_type': 'character varying',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': 'false',
                        'column_name': 'mfa_enabled',
                        'data_type': 'boolean',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'mfa_secret',
                        'data_type': 'text',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'quota_grams',
                        'data_type': 'real',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'quota_hours',
                        'data_type': 'real',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'quota_jobs',
                        'data_type': 'integer',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': "'monthly'::character varying",
                        'column_name': 'quota_period',
                        'data_type': 'character varying',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'theme_json',
                        'data_type': 'text',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'group_id',
                        'data_type': 'integer',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'oidc_issuer',
                        'data_type': 'character varying',
                        'is_nullable': 'YES'},
                       {'collation_name': None,
                        'column_default': None,
                        'column_name': 'account_nonce',
                        'data_type': 'text',
                        'is_nullable': 'YES'}]},
 'constraints': {'account_auth_activation': [{'condeferrable': False,
                                              'condeferred': False,
                                              'conname': 'account_auth_activation_pkey',
                                              'contype': 'p',
                                              'convalidated': True,
                                              'definition': 'PRIMARY KEY (singleton)',
                                              'expression': None},
                                             {'condeferrable': False,
                                              'condeferred': False,
                                              'conname': 'acv_singleton',
                                              'contype': 'c',
                                              'convalidated': True,
                                              'definition': 'CHECK ((singleton = 1))',
                                              'expression': '(singleton = 1)'},
                                             {'condeferrable': False,
                                              'condeferred': False,
                                              'conname': 'acv_time',
                                              'contype': 'c',
                                              'convalidated': True,
                                              'definition': 'CHECK ((isfinite(activated_at) AND ((EXTRACT(year FROM '
                                                            "(activated_at AT TIME ZONE 'UTC'::text)) >= (1)::numeric) "
                                                            'AND (EXTRACT(year FROM (activated_at AT TIME ZONE '
                                                            "'UTC'::text)) <= (9999)::numeric)) AND "
                                                            "(date_trunc('milliseconds'::text, activated_at) = "
                                                            'activated_at)))',
                                              'expression': '(isfinite(activated_at) AND ((EXTRACT(year FROM '
                                                            "(activated_at AT TIME ZONE 'UTC'::text)) >= (1)::numeric) "
                                                            'AND (EXTRACT(year FROM (activated_at AT TIME ZONE '
                                                            "'UTC'::text)) <= (9999)::numeric)) AND "
                                                            "(date_trunc('milliseconds'::text, activated_at) = "
                                                            'activated_at))'},
                                             {'condeferrable': False,
                                              'condeferred': False,
                                              'conname': 'acv_version',
                                              'contype': 'c',
                                              'convalidated': True,
                                              'definition': 'CHECK ((version = 2))',
                                              'expression': '(version = 2)'}],
                 'account_authorities': [{'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'aca_expires',
                                          'contype': 'c',
                                          'convalidated': True,
                                          'definition': 'CHECK ((isfinite(expires_at) AND ((EXTRACT(year FROM '
                                                        "(expires_at AT TIME ZONE 'UTC'::text)) >= (1)::numeric) AND "
                                                        "(EXTRACT(year FROM (expires_at AT TIME ZONE 'UTC'::text)) <= "
                                                        "(9999)::numeric)) AND (date_trunc('milliseconds'::text, "
                                                        'expires_at) = expires_at)))',
                                          'expression': '(isfinite(expires_at) AND ((EXTRACT(year FROM (expires_at AT '
                                                        "TIME ZONE 'UTC'::text)) >= (1)::numeric) AND (EXTRACT(year "
                                                        "FROM (expires_at AT TIME ZONE 'UTC'::text)) <= "
                                                        "(9999)::numeric)) AND (date_trunc('milliseconds'::text, "
                                                        'expires_at) = expires_at))'},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'aca_issued',
                                          'contype': 'c',
                                          'convalidated': True,
                                          'definition': 'CHECK ((isfinite(issued_at) AND ((EXTRACT(year FROM '
                                                        "(issued_at AT TIME ZONE 'UTC'::text)) >= (1)::numeric) AND "
                                                        "(EXTRACT(year FROM (issued_at AT TIME ZONE 'UTC'::text)) <= "
                                                        "(9999)::numeric)) AND (date_trunc('milliseconds'::text, "
                                                        'issued_at) = issued_at)))',
                                          'expression': '(isfinite(issued_at) AND ((EXTRACT(year FROM (issued_at AT '
                                                        "TIME ZONE 'UTC'::text)) >= (1)::numeric) AND (EXTRACT(year "
                                                        "FROM (issued_at AT TIME ZONE 'UTC'::text)) <= "
                                                        "(9999)::numeric)) AND (date_trunc('milliseconds'::text, "
                                                        'issued_at) = issued_at))'},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'aca_jti',
                                          'contype': 'c',
                                          'convalidated': True,
                                          'definition': 'CHECK ((((octet_length(jti) >= 1) AND (octet_length(jti) <= '
                                                        '128)) AND ((jti COLLATE "C") ~ \'^[!-~]+$\'::text)))',
                                          'expression': '(((octet_length(jti) >= 1) AND (octet_length(jti) <= 128)) '
                                                        'AND ((jti COLLATE "C") ~ \'^[!-~]+$\'::text))'},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'aca_nonce',
                                          'contype': 'c',
                                          'convalidated': True,
                                          'definition': 'CHECK (((octet_length(account_nonce) = 32) AND '
                                                        '((account_nonce COLLATE "C") ~ \'^[0-9a-f]{32}$\'::text)))',
                                          'expression': '((octet_length(account_nonce) = 32) AND ((account_nonce '
                                                        'COLLATE "C") ~ \'^[0-9a-f]{32}$\'::text))'},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'aca_order',
                                          'contype': 'c',
                                          'convalidated': True,
                                          'definition': 'CHECK ((expires_at > issued_at))',
                                          'expression': '(expires_at > issued_at)'},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'aca_purpose',
                                          'contype': 'c',
                                          'convalidated': True,
                                          'definition': "CHECK ((purpose = ANY (ARRAY['access'::text, "
                                                        "'mfa_login'::text, 'mfa_enrollment'::text, 'websocket'::text, "
                                                        "'oidc_pending'::text])))",
                                          'expression': "(purpose = ANY (ARRAY['access'::text, 'mfa_login'::text, "
                                                        "'mfa_enrollment'::text, 'websocket'::text, "
                                                        "'oidc_pending'::text]))"},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'account_authorities_account_nonce_fkey',
                                          'contype': 'f',
                                          'convalidated': True,
                                          'definition': 'FOREIGN KEY (account_nonce) REFERENCES '
                                                        'account_lifetimes(account_nonce)',
                                          'expression': None},
                                         {'condeferrable': False,
                                          'condeferred': False,
                                          'conname': 'account_authorities_pkey',
                                          'contype': 'p',
                                          'convalidated': True,
                                          'definition': 'PRIMARY KEY (jti)',
                                          'expression': None}],
                 'account_bindings': [{'condeferrable': False,
                                       'condeferred': False,
                                       'conname': 'acb_nonce',
                                       'contype': 'c',
                                       'convalidated': True,
                                       'definition': 'CHECK (((octet_length(account_nonce) = 32) AND ((account_nonce '
                                                     'COLLATE "C") ~ \'^[0-9a-f]{32}$\'::text)))',
                                       'expression': '((octet_length(account_nonce) = 32) AND ((account_nonce COLLATE '
                                                     '"C") ~ \'^[0-9a-f]{32}$\'::text))'},
                                      {'condeferrable': False,
                                       'condeferred': False,
                                       'conname': 'acb_user',
                                       'contype': 'c',
                                       'convalidated': True,
                                       'definition': 'CHECK (((user_id >= 1) AND (user_id <= 2147483647)))',
                                       'expression': '((user_id >= 1) AND (user_id <= 2147483647))'},
                                      {'condeferrable': True,
                                       'condeferred': True,
                                       'conname': 'account_bindings_account_nonce_user_id_fkey',
                                       'contype': 'f',
                                       'convalidated': True,
                                       'definition': 'FOREIGN KEY (account_nonce, user_id) REFERENCES '
                                                     'account_lifetimes(account_nonce, current_user_id) DEFERRABLE '
                                                     'INITIALLY DEFERRED',
                                       'expression': None},
                                      {'condeferrable': False,
                                       'condeferred': False,
                                       'conname': 'account_bindings_pkey',
                                       'contype': 'p',
                                       'convalidated': True,
                                       'definition': 'PRIMARY KEY (account_nonce)',
                                       'expression': None},
                                      {'condeferrable': False,
                                       'condeferred': False,
                                       'conname': 'account_bindings_user_id_key',
                                       'contype': 'u',
                                       'convalidated': True,
                                       'definition': 'UNIQUE (user_id)',
                                       'expression': None}],
                 'account_lifetimes': [{'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'account_lifetimes_account_nonce_current_user_id_key',
                                        'contype': 'u',
                                        'convalidated': True,
                                        'definition': 'UNIQUE (account_nonce, current_user_id)',
                                        'expression': None},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'account_lifetimes_current_user_id_key',
                                        'contype': 'u',
                                        'convalidated': True,
                                        'definition': 'UNIQUE (current_user_id)',
                                        'expression': None},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'account_lifetimes_pkey',
                                        'contype': 'p',
                                        'convalidated': True,
                                        'definition': 'PRIMARY KEY (account_nonce)',
                                        'expression': None},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'acl_birth_id',
                                        'contype': 'c',
                                        'convalidated': True,
                                        'definition': 'CHECK (((birth_user_id >= 1) AND (birth_user_id <= '
                                                      '2147483647)))',
                                        'expression': '((birth_user_id >= 1) AND (birth_user_id <= 2147483647))'},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'acl_birth_time',
                                        'contype': 'c',
                                        'convalidated': True,
                                        'definition': 'CHECK ((isfinite(born_at) AND ((EXTRACT(year FROM (born_at AT '
                                                      "TIME ZONE 'UTC'::text)) >= (1)::numeric) AND (EXTRACT(year FROM "
                                                      "(born_at AT TIME ZONE 'UTC'::text)) <= (9999)::numeric)) AND "
                                                      "(date_trunc('milliseconds'::text, born_at) = born_at)))",
                                        'expression': '(isfinite(born_at) AND ((EXTRACT(year FROM (born_at AT TIME '
                                                      "ZONE 'UTC'::text)) >= (1)::numeric) AND (EXTRACT(year FROM "
                                                      "(born_at AT TIME ZONE 'UTC'::text)) <= (9999)::numeric)) AND "
                                                      "(date_trunc('milliseconds'::text, born_at) = born_at))"},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'acl_current_id',
                                        'contype': 'c',
                                        'convalidated': True,
                                        'definition': 'CHECK (((current_user_id IS NULL) OR ((current_user_id >= 1) '
                                                      'AND (current_user_id <= 2147483647))))',
                                        'expression': '((current_user_id IS NULL) OR ((current_user_id >= 1) AND '
                                                      '(current_user_id <= 2147483647)))'},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'acl_nonce',
                                        'contype': 'c',
                                        'convalidated': True,
                                        'definition': 'CHECK (((octet_length(account_nonce) = 32) AND ((account_nonce '
                                                      'COLLATE "C") ~ \'^[0-9a-f]{32}$\'::text)))',
                                        'expression': '((octet_length(account_nonce) = 32) AND ((account_nonce COLLATE '
                                                      '"C") ~ \'^[0-9a-f]{32}$\'::text))'},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'acl_retire_time',
                                        'contype': 'c',
                                        'convalidated': True,
                                        'definition': 'CHECK (((retired_at IS NULL) OR (isfinite(retired_at) AND '
                                                      "((EXTRACT(year FROM (retired_at AT TIME ZONE 'UTC'::text)) >= "
                                                      '(1)::numeric) AND (EXTRACT(year FROM (retired_at AT TIME ZONE '
                                                      "'UTC'::text)) <= (9999)::numeric)) AND "
                                                      "(date_trunc('milliseconds'::text, retired_at) = retired_at))))",
                                        'expression': '((retired_at IS NULL) OR (isfinite(retired_at) AND '
                                                      "((EXTRACT(year FROM (retired_at AT TIME ZONE 'UTC'::text)) >= "
                                                      '(1)::numeric) AND (EXTRACT(year FROM (retired_at AT TIME ZONE '
                                                      "'UTC'::text)) <= (9999)::numeric)) AND "
                                                      "(date_trunc('milliseconds'::text, retired_at) = retired_at)))"},
                                       {'condeferrable': False,
                                        'condeferred': False,
                                        'conname': 'acl_state',
                                        'contype': 'c',
                                        'convalidated': True,
                                        'definition': "CHECK ((((state = 'active'::text) AND (current_user_id IS NOT "
                                                      'NULL) AND (current_user_id = birth_user_id) AND (retired_at IS '
                                                      "NULL)) OR ((state = 'retired'::text) AND (current_user_id IS "
                                                      'NULL) AND (retired_at IS NOT NULL) AND (retired_at >= '
                                                      'born_at))))',
                                        'expression': "(((state = 'active'::text) AND (current_user_id IS NOT NULL) "
                                                      'AND (current_user_id = birth_user_id) AND (retired_at IS NULL)) '
                                                      "OR ((state = 'retired'::text) AND (current_user_id IS NULL) AND "
                                                      '(retired_at IS NOT NULL) AND (retired_at >= born_at)))'},
                                       {'condeferrable': True,
                                        'condeferred': True,
                                        'conname': 'fk_account_lifetimes_users',
                                        'contype': 'f',
                                        'convalidated': True,
                                        'definition': 'FOREIGN KEY (current_user_id, account_nonce) REFERENCES '
                                                      'users(id, account_nonce) DEFERRABLE INITIALLY DEFERRED',
                                        'expression': None}],
                 'account_retirement_receipts': [{'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'account_retirement_receipts_account_nonce_fkey',
                                                  'contype': 'f',
                                                  'convalidated': True,
                                                  'definition': 'FOREIGN KEY (account_nonce) REFERENCES '
                                                                'account_lifetimes(account_nonce)',
                                                  'expression': None},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'account_retirement_receipts_account_nonce_key',
                                                  'contype': 'u',
                                                  'convalidated': True,
                                                  'definition': 'UNIQUE (account_nonce)',
                                                  'expression': None},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'account_retirement_receipts_actor_account_nonce_fkey',
                                                  'contype': 'f',
                                                  'convalidated': True,
                                                  'definition': 'FOREIGN KEY (actor_account_nonce) REFERENCES '
                                                                'account_lifetimes(account_nonce)',
                                                  'expression': None},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'account_retirement_receipts_pkey',
                                                  'contype': 'p',
                                                  'convalidated': True,
                                                  'definition': 'PRIMARY KEY (operation_nonce)',
                                                  'expression': None},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'acr_actor',
                                                  'contype': 'c',
                                                  'convalidated': True,
                                                  'definition': 'CHECK (((octet_length(actor_account_nonce) = 32) AND '
                                                                '((actor_account_nonce COLLATE "C") ~ '
                                                                "'^[0-9a-f]{32}$'::text)))",
                                                  'expression': '((octet_length(actor_account_nonce) = 32) AND '
                                                                '((actor_account_nonce COLLATE "C") ~ '
                                                                "'^[0-9a-f]{32}$'::text))"},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'acr_digest',
                                                  'contype': 'c',
                                                  'convalidated': True,
                                                  'definition': 'CHECK (((octet_length(request_sha256) = 64) AND '
                                                                '((request_sha256 COLLATE "C") ~ '
                                                                "'^[0-9a-f]{64}$'::text)))",
                                                  'expression': '((octet_length(request_sha256) = 64) AND '
                                                                '((request_sha256 COLLATE "C") ~ '
                                                                "'^[0-9a-f]{64}$'::text))"},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'acr_operation',
                                                  'contype': 'c',
                                                  'convalidated': True,
                                                  'definition': 'CHECK (((octet_length(operation_nonce) = 32) AND '
                                                                '((operation_nonce COLLATE "C") ~ '
                                                                "'^[0-9a-f]{32}$'::text)))",
                                                  'expression': '((octet_length(operation_nonce) = 32) AND '
                                                                '((operation_nonce COLLATE "C") ~ '
                                                                "'^[0-9a-f]{32}$'::text))"},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'acr_result',
                                                  'contype': 'c',
                                                  'convalidated': True,
                                                  'definition': "CHECK ((result = 'retired'::text))",
                                                  'expression': "(result = 'retired'::text)"},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'acr_target',
                                                  'contype': 'c',
                                                  'convalidated': True,
                                                  'definition': 'CHECK (((octet_length(account_nonce) = 32) AND '
                                                                '((account_nonce COLLATE "C") ~ '
                                                                "'^[0-9a-f]{32}$'::text)))",
                                                  'expression': '((octet_length(account_nonce) = 32) AND '
                                                                '((account_nonce COLLATE "C") ~ '
                                                                "'^[0-9a-f]{32}$'::text))"},
                                                 {'condeferrable': False,
                                                  'condeferred': False,
                                                  'conname': 'acr_time',
                                                  'contype': 'c',
                                                  'convalidated': True,
                                                  'definition': 'CHECK ((isfinite(retired_at) AND ((EXTRACT(year FROM '
                                                                "(retired_at AT TIME ZONE 'UTC'::text)) >= "
                                                                '(1)::numeric) AND (EXTRACT(year FROM (retired_at AT '
                                                                "TIME ZONE 'UTC'::text)) <= (9999)::numeric)) AND "
                                                                "(date_trunc('milliseconds'::text, retired_at) = "
                                                                'retired_at)))',
                                                  'expression': '(isfinite(retired_at) AND ((EXTRACT(year FROM '
                                                                "(retired_at AT TIME ZONE 'UTC'::text)) >= "
                                                                '(1)::numeric) AND (EXTRACT(year FROM (retired_at AT '
                                                                "TIME ZONE 'UTC'::text)) <= (9999)::numeric)) AND "
                                                                "(date_trunc('milliseconds'::text, retired_at) = "
                                                                'retired_at))'}],
                 'users': [{'condeferrable': False,
                            'condeferred': False,
                            'conname': 'users_account_nonce_check',
                            'contype': 'c',
                            'convalidated': True,
                            'definition': 'CHECK (((account_nonce IS NULL) OR ((octet_length(account_nonce) = 32) AND '
                                          '((account_nonce COLLATE "C") ~ \'^[0-9a-f]{32}$\'::text))))',
                            'expression': '((account_nonce IS NULL) OR ((octet_length(account_nonce) = 32) AND '
                                          '((account_nonce COLLATE "C") ~ \'^[0-9a-f]{32}$\'::text)))'},
                           {'condeferrable': True,
                            'condeferred': True,
                            'conname': 'users_account_nonce_fkey',
                            'contype': 'f',
                            'convalidated': True,
                            'definition': 'FOREIGN KEY (account_nonce) REFERENCES account_bindings(account_nonce) '
                                          'DEFERRABLE INITIALLY DEFERRED',
                            'expression': None},
                           {'condeferrable': False,
                            'condeferred': False,
                            'conname': 'users_pkey',
                            'contype': 'p',
                            'convalidated': True,
                            'definition': 'PRIMARY KEY (id)',
                            'expression': None},
                           {'condeferrable': False,
                            'condeferred': False,
                            'conname': 'users_username_key',
                            'contype': 'u',
                            'convalidated': True,
                            'definition': 'UNIQUE (username)',
                            'expression': None}]},
 'indexes': {'account_auth_activation': [{'indexdef': 'CREATE UNIQUE INDEX account_auth_activation_pkey ON '
                                                      'public.account_auth_activation USING btree (singleton)',
                                          'indexname': 'account_auth_activation_pkey',
                                          'indimmediate': True,
                                          'indisready': True,
                                          'indisvalid': True}],
             'account_authorities': [{'indexdef': 'CREATE UNIQUE INDEX account_authorities_pkey ON '
                                                  'public.account_authorities USING btree (jti)',
                                      'indexname': 'account_authorities_pkey',
                                      'indimmediate': True,
                                      'indisready': True,
                                      'indisvalid': True}],
             'account_bindings': [{'indexdef': 'CREATE UNIQUE INDEX account_bindings_pkey ON public.account_bindings '
                                               'USING btree (account_nonce)',
                                   'indexname': 'account_bindings_pkey',
                                   'indimmediate': True,
                                   'indisready': True,
                                   'indisvalid': True},
                                  {'indexdef': 'CREATE UNIQUE INDEX account_bindings_user_id_key ON '
                                               'public.account_bindings USING btree (user_id)',
                                   'indexname': 'account_bindings_user_id_key',
                                   'indimmediate': True,
                                   'indisready': True,
                                   'indisvalid': True}],
             'account_lifetimes': [{'indexdef': 'CREATE UNIQUE INDEX '
                                                'account_lifetimes_account_nonce_current_user_id_key ON '
                                                'public.account_lifetimes USING btree (account_nonce, current_user_id)',
                                    'indexname': 'account_lifetimes_account_nonce_current_user_id_key',
                                    'indimmediate': True,
                                    'indisready': True,
                                    'indisvalid': True},
                                   {'indexdef': 'CREATE UNIQUE INDEX account_lifetimes_current_user_id_key ON '
                                                'public.account_lifetimes USING btree (current_user_id)',
                                    'indexname': 'account_lifetimes_current_user_id_key',
                                    'indimmediate': True,
                                    'indisready': True,
                                    'indisvalid': True},
                                   {'indexdef': 'CREATE UNIQUE INDEX account_lifetimes_pkey ON '
                                                'public.account_lifetimes USING btree (account_nonce)',
                                    'indexname': 'account_lifetimes_pkey',
                                    'indimmediate': True,
                                    'indisready': True,
                                    'indisvalid': True}],
             'account_retirement_receipts': [{'indexdef': 'CREATE UNIQUE INDEX '
                                                          'account_retirement_receipts_account_nonce_key ON '
                                                          'public.account_retirement_receipts USING btree '
                                                          '(account_nonce)',
                                              'indexname': 'account_retirement_receipts_account_nonce_key',
                                              'indimmediate': True,
                                              'indisready': True,
                                              'indisvalid': True},
                                             {'indexdef': 'CREATE UNIQUE INDEX account_retirement_receipts_pkey ON '
                                                          'public.account_retirement_receipts USING btree '
                                                          '(operation_nonce)',
                                              'indexname': 'account_retirement_receipts_pkey',
                                              'indimmediate': True,
                                              'indisready': True,
                                              'indisvalid': True}],
             'users': [{'indexdef': 'CREATE UNIQUE INDEX uq_users_account_nonce ON public.users USING btree '
                                    '(account_nonce)',
                        'indexname': 'uq_users_account_nonce',
                        'indimmediate': True,
                        'indisready': True,
                        'indisvalid': True},
                       {'indexdef': 'CREATE UNIQUE INDEX uq_users_id_account_nonce ON public.users USING btree (id, '
                                    'account_nonce)',
                        'indexname': 'uq_users_id_account_nonce',
                        'indimmediate': True,
                        'indisready': True,
                        'indisvalid': True},
                       {'indexdef': 'CREATE UNIQUE INDEX uq_users_id_group ON public.users USING btree (id, group_id)',
                        'indexname': 'uq_users_id_group',
                        'indimmediate': True,
                        'indisready': True,
                        'indisvalid': True},
                       {'indexdef': 'CREATE UNIQUE INDEX uq_users_oidc_identity ON public.users USING btree '
                                    '(oidc_issuer, oidc_subject) WHERE ((oidc_issuer IS NOT NULL) AND (oidc_subject IS '
                                    'NOT NULL))',
                        'indexname': 'uq_users_oidc_identity',
                        'indimmediate': True,
                        'indisready': True,
                        'indisvalid': True},
                       {'indexdef': 'CREATE UNIQUE INDEX users_pkey ON public.users USING btree (id)',
                        'indexname': 'users_pkey',
                        'indimmediate': True,
                        'indisready': True,
                        'indisvalid': True},
                       {'indexdef': 'CREATE UNIQUE INDEX users_username_key ON public.users USING btree (username)',
                        'indexname': 'users_username_key',
                        'indimmediate': True,
                        'indisready': True,
                        'indisvalid': True}]},
 'oid_references': {'account_auth_activation': [],
                    'account_authorities': [{'confkey': [1],
                                             'conkey': [2],
                                             'conname': 'account_authorities_account_nonce_fkey',
                                             'target_schema': 'public',
                                             'target_table': 'account_lifetimes'}],
                    'account_bindings': [{'confkey': [1, 3],
                                          'conkey': [1, 2],
                                          'conname': 'account_bindings_account_nonce_user_id_fkey',
                                          'target_schema': 'public',
                                          'target_table': 'account_lifetimes'}],
                    'account_lifetimes': [{'confkey': [1, 20],
                                           'conkey': [3, 1],
                                           'conname': 'fk_account_lifetimes_users',
                                           'target_schema': 'public',
                                           'target_table': 'users'}],
                    'account_retirement_receipts': [{'confkey': [1],
                                                     'conkey': [2],
                                                     'conname': 'account_retirement_receipts_account_nonce_fkey',
                                                     'target_schema': 'public',
                                                     'target_table': 'account_lifetimes'},
                                                    {'confkey': [1],
                                                     'conkey': [3],
                                                     'conname': 'account_retirement_receipts_actor_account_nonce_fkey',
                                                     'target_schema': 'public',
                                                     'target_table': 'account_lifetimes'}],
                    'users': [{'confkey': [1],
                               'conkey': [20],
                               'conname': 'users_account_nonce_fkey',
                               'target_schema': 'public',
                               'target_table': 'account_bindings'}]},
 'triggers': {'account_auth_activation': [{'definition': 'CREATE TRIGGER account_028_auth_activation_lifecycle BEFORE '
                                                         'INSERT OR DELETE OR UPDATE ON public.account_auth_activation '
                                                         'FOR EACH ROW EXECUTE FUNCTION '
                                                         'account_028_auth_activation_guard()',
                                           'function_schema': 'public',
                                           'proname': 'account_028_auth_activation_guard',
                                           'tgenabled': 'O',
                                           'tgname': 'account_028_auth_activation_lifecycle',
                                           'tgtype': 31},
                                          {'definition': 'CREATE TRIGGER account_028_auth_activation_truncate BEFORE '
                                                         'TRUNCATE ON public.account_auth_activation FOR EACH '
                                                         'STATEMENT EXECUTE FUNCTION '
                                                         'account_028_auth_activation_guard()',
                                           'function_schema': 'public',
                                           'proname': 'account_028_auth_activation_guard',
                                           'tgenabled': 'O',
                                           'tgname': 'account_028_auth_activation_truncate',
                                           'tgtype': 34}],
              'account_authorities': [{'definition': 'CREATE TRIGGER account_028_authorities_lifecycle BEFORE INSERT '
                                                     'OR DELETE OR UPDATE ON public.account_authorities FOR EACH ROW '
                                                     'EXECUTE FUNCTION account_028_authorities_guard()',
                                       'function_schema': 'public',
                                       'proname': 'account_028_authorities_guard',
                                       'tgenabled': 'O',
                                       'tgname': 'account_028_authorities_lifecycle',
                                       'tgtype': 31},
                                      {'definition': 'CREATE TRIGGER account_028_authorities_truncate BEFORE TRUNCATE '
                                                     'ON public.account_authorities FOR EACH STATEMENT EXECUTE '
                                                     'FUNCTION account_028_authorities_guard()',
                                       'function_schema': 'public',
                                       'proname': 'account_028_authorities_guard',
                                       'tgenabled': 'O',
                                       'tgname': 'account_028_authorities_truncate',
                                       'tgtype': 34}],
              'account_bindings': [{'definition': 'CREATE TRIGGER account_028_bindings_lifecycle BEFORE INSERT OR '
                                                  'DELETE OR UPDATE ON public.account_bindings FOR EACH ROW EXECUTE '
                                                  'FUNCTION account_028_bindings_guard()',
                                    'function_schema': 'public',
                                    'proname': 'account_028_bindings_guard',
                                    'tgenabled': 'O',
                                    'tgname': 'account_028_bindings_lifecycle',
                                    'tgtype': 31},
                                   {'definition': 'CREATE TRIGGER account_028_bindings_truncate BEFORE TRUNCATE ON '
                                                  'public.account_bindings FOR EACH STATEMENT EXECUTE FUNCTION '
                                                  'account_028_bindings_guard()',
                                    'function_schema': 'public',
                                    'proname': 'account_028_bindings_guard',
                                    'tgenabled': 'O',
                                    'tgname': 'account_028_bindings_truncate',
                                    'tgtype': 34}],
              'account_lifetimes': [{'definition': 'CREATE TRIGGER account_028_lifetimes_lifecycle BEFORE INSERT OR '
                                                   'DELETE OR UPDATE ON public.account_lifetimes FOR EACH ROW EXECUTE '
                                                   'FUNCTION account_028_lifetimes_guard()',
                                     'function_schema': 'public',
                                     'proname': 'account_028_lifetimes_guard',
                                     'tgenabled': 'O',
                                     'tgname': 'account_028_lifetimes_lifecycle',
                                     'tgtype': 31},
                                    {'definition': 'CREATE TRIGGER account_028_lifetimes_truncate BEFORE TRUNCATE ON '
                                                   'public.account_lifetimes FOR EACH STATEMENT EXECUTE FUNCTION '
                                                   'account_028_lifetimes_guard()',
                                     'function_schema': 'public',
                                     'proname': 'account_028_lifetimes_guard',
                                     'tgenabled': 'O',
                                     'tgname': 'account_028_lifetimes_truncate',
                                     'tgtype': 34}],
              'account_retirement_receipts': [{'definition': 'CREATE TRIGGER account_028_retirement_receipts_lifecycle '
                                                             'BEFORE INSERT OR DELETE OR UPDATE ON '
                                                             'public.account_retirement_receipts FOR EACH ROW EXECUTE '
                                                             'FUNCTION account_028_retirement_receipts_guard()',
                                               'function_schema': 'public',
                                               'proname': 'account_028_retirement_receipts_guard',
                                               'tgenabled': 'O',
                                               'tgname': 'account_028_retirement_receipts_lifecycle',
                                               'tgtype': 31},
                                              {'definition': 'CREATE TRIGGER account_028_retirement_receipts_truncate '
                                                             'BEFORE TRUNCATE ON public.account_retirement_receipts '
                                                             'FOR EACH STATEMENT EXECUTE FUNCTION '
                                                             'account_028_retirement_receipts_guard()',
                                               'function_schema': 'public',
                                               'proname': 'account_028_retirement_receipts_guard',
                                               'tgenabled': 'O',
                                               'tgname': 'account_028_retirement_receipts_truncate',
                                               'tgtype': 34}],
              'users': [{'definition': 'CREATE TRIGGER account_028_users_lifecycle BEFORE INSERT OR UPDATE ON '
                                       'public.users FOR EACH ROW EXECUTE FUNCTION account_028_users_guard()',
                         'function_schema': 'public',
                         'proname': 'account_028_users_guard',
                         'tgenabled': 'O',
                         'tgname': 'account_028_users_lifecycle',
                         'tgtype': 23},
                        {'definition': 'CREATE TRIGGER account_028_users_truncate BEFORE TRUNCATE ON public.users FOR '
                                       'EACH STATEMENT EXECUTE FUNCTION account_028_users_guard()',
                         'function_schema': 'public',
                         'proname': 'account_028_users_guard',
                         'tgenabled': 'O',
                         'tgname': 'account_028_users_truncate',
                         'tgtype': 34}]}}
