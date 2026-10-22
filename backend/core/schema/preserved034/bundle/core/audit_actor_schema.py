"""Prospective raw audit fields; deliberately outside frozen ORM metadata."""
import re
from datetime import datetime
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

ACTOR_COLUMNS = ('actor_user_id', 'actor_username', 'actor_auth_kind')
HUMAN_KINDS = ('session_jwt','bearer_jwt','user_api_token','password_login','mfa_login')
LEDGER = 'audit_actor_erasures'
FUNCTION = 'audit_actor_erasure_immutable'

def checks(pg):
    definitions = {
        'audit_actor_id': 'actor_user_id IS NULL OR (actor_user_id BETWEEN 1 AND 2147483647'+('' if pg else " AND typeof(actor_user_id)='integer'")+')',
        'audit_actor_name': 'actor_username IS NULL OR (length(actor_username) BETWEEN 1 AND 128'+('' if pg else " AND typeof(actor_username)='text'")+')',
        'audit_actor_bundle': "(actor_user_id IS NULL AND actor_username IS NULL AND actor_auth_kind IS NULL) OR (actor_user_id IS NOT NULL AND actor_username IS NOT NULL AND actor_auth_kind IS NOT NULL AND actor_auth_kind IN ('session_jwt','bearer_jwt','user_api_token','password_login','mfa_login')) OR (actor_user_id IS NULL AND actor_username IS NULL AND actor_auth_kind IS NOT NULL AND actor_auth_kind='legacy_global_api_key')",
    }
    if pg:definitions['audit_actor_bundle']=definitions['audit_actor_bundle'].replace('actor_auth_kind IN (','CAST(actor_auth_kind AS TEXT) IN (')
    return definitions

def ledger_ddl(pg, name=LEDGER, temporary=False):
    idcheck='user_id BETWEEN 1 AND 2147483647'+('' if pg else " AND typeof(user_id)='integer'")
    timecheck='isfinite(erased_at)' if pg else "typeof(erased_at)='text' AND length(erased_at)=19 AND strftime('%Y-%m-%d %H:%M:%S',erased_at) IS NOT NULL AND strftime('%Y-%m-%d %H:%M:%S',erased_at)=erased_at"
    timestamp="TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT timezone('UTC',CURRENT_TIMESTAMP)" if pg else 'DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'
    return f"CREATE {'TEMPORARY ' if temporary else ''}TABLE {name} (user_id INTEGER PRIMARY KEY NOT NULL CONSTRAINT audit_actor_erasure_id CHECK({idcheck}), erased_at {timestamp} CONSTRAINT audit_actor_erasure_time CHECK({timecheck}))"

PG_BODY="BEGIN RAISE EXCEPTION 'audit actor erasure is immutable'; END"

def trigger_sql(pg):
    if pg:
        return {
            'audit_actor_erasures_no_change': f'CREATE TRIGGER audit_actor_erasures_no_change BEFORE UPDATE OR DELETE ON {LEDGER} FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()',
            'audit_actor_erasures_no_truncate': f'CREATE TRIGGER audit_actor_erasures_no_truncate BEFORE TRUNCATE ON {LEDGER} FOR EACH STATEMENT EXECUTE FUNCTION {FUNCTION}()',
        }
    return {f'audit_actor_erasures_no_{e.lower()}':f"CREATE TRIGGER audit_actor_erasures_no_{e.lower()} BEFORE {e} ON {LEDGER} BEGIN SELECT RAISE(ABORT,'audit actor erasure is immutable'); END" for e in ('UPDATE','DELETE')}

def normal(value):
    # Ignore formatting only outside string literals; vocabulary is case-sensitive.
    tokens=re.findall(r"'(?:''|[^'])*'|[^']+",str(value))
    return ''.join(t if t.startswith("'") else re.sub(r'\s+','',t).lower() for t in tokens).rstrip(';')

def _base(connection, added):
    from core.models import AuditLog
    actual=inspect(connection)
    columns=actual.get_columns('audit_logs')
    expected=AuditLog.__table__.columns
    if set(c['name'] for c in columns)!=set(expected.keys()) | (set(ACTOR_COLUMNS) if added else set()):raise MigrationError('Audit original columns differ')
    for c in columns:
        if c['name'] not in expected:continue
        e=expected[c['name']]
        if str(c['type'].compile(dialect=connection.dialect)).upper()!=str(e.type.compile(dialect=connection.dialect)).upper() or c['nullable']!=e.nullable:raise MigrationError('Audit original column shape differs')
        default=normal(c['default'])
        allowed={'none'} if e.server_default is None else ({'current_timestamp'} if connection.dialect.name=='sqlite' else {'now()'})
        if connection.dialect.name=='postgresql' and c['name']=='id':
            seq=connection.execute(text("SELECT pg_get_serial_sequence('audit_logs','id')")).scalar_one()
            if seq not in {'public.audit_logs_id_seq','audit_logs_id_seq'}:raise MigrationError('Audit original sequence differs')
            allowed={"nextval('audit_logs_id_seq'::regclass)","nextval('public.audit_logs_id_seq'::regclass)"}
        if default not in allowed:raise MigrationError('Audit original default differs')
    if actual.get_pk_constraint('audit_logs')['constrained_columns']!=['id'] or actual.get_foreign_keys('audit_logs') or actual.get_unique_constraints('audit_logs'):raise MigrationError('Audit original keys differ')
    if actual.get_indexes('audit_logs'):raise MigrationError('Audit original indexes differ')


def _pg_checks(connection, table):
    rows=connection.execute(text("SELECT conname,pg_get_constraintdef(oid),convalidated,condeferrable,condeferred FROM pg_constraint WHERE conrelid=to_regclass(:t) AND contype='c'"),{'t':table}).all()
    if any(not r[2] or r[3] or r[4] for r in rows):raise MigrationError('Audit checks are not validated immediate constraints')
    return {r[0]:r[1] for r in rows}


def _boundary(connection):
    if connection.dialect.name=='postgresql':
        if connection.execute(text('SELECT current_schema()')).scalar_one()!='public':raise MigrationError('Audit actor schema differs')
        for table in ('audit_logs',LEDGER):
            oid,qualified=connection.execute(text('SELECT to_regclass(:t)::oid,to_regclass(:q)::oid'),{'t':table,'q':'public.'+table}).one()
            if oid!=qualified:raise MigrationError('Audit actor relation shadowed')
            if oid is not None:
                if connection.execute(text('SELECT relkind,relpersistence FROM pg_class WHERE oid=:oid'),{'oid':oid}).one()!=('r','p'):raise MigrationError('Audit actor relation differs')
                connection.exec_driver_sql('LOCK TABLE public.'+table+' IN SHARE MODE NOWAIT')
    elif connection.dialect.name=='sqlite':
        if not connection.connection.driver_connection.in_transaction:raise MigrationError('Audit actor requires SQLite writer boundary')
        if connection.execute(text("SELECT 1 FROM sqlite_temp_master WHERE name IN ('audit_logs','audit_actor_erasures')")).first():raise MigrationError('Audit actor relation shadowed')
        connection.execute(text('UPDATE audit_logs SET id=id WHERE 0'))
    else:raise MigrationError('Unsupported audit actor dialect')

def validate(connection):
    _boundary(connection)
    pg=connection.dialect.name=='postgresql'
    _base(connection,True)
    inspector=inspect(connection)
    cols={c['name']:c for c in inspector.get_columns('audit_logs')}
    for name,kind in zip(ACTOR_COLUMNS,('INTEGER','VARCHAR(128)','VARCHAR(32)')):
        c=cols[name]
        if str(c['type']).upper()!=kind or not c['nullable'] or c['default'] is not None:raise MigrationError('Audit actor column differs')
    if not inspector.has_table(LEDGER):raise MigrationError('Audit erasure ledger missing')
    lc=inspector.get_columns(LEDGER)
    if [c['name'] for c in lc]!=['user_id','erased_at'] or any(c['nullable'] for c in lc) or str(lc[0]['type']).upper()!='INTEGER' or lc[0]['default'] is not None:raise MigrationError('Audit ledger shape differs')
    if inspector.get_pk_constraint(LEDGER)['constrained_columns']!=['user_id'] or inspector.get_foreign_keys(LEDGER) or inspector.get_unique_constraints(LEDGER) or inspector.get_indexes(LEDGER):raise MigrationError('Audit ledger keys differ')
    if pg:
        # Server deparser is compared against exact source-created private TEMP contracts.
        connection.execute(text('CREATE TEMP TABLE audit_actor_reference (actor_user_id INTEGER,actor_username VARCHAR(128),actor_auth_kind VARCHAR(32))'))
        connection.execute(text(ledger_ddl(True,'audit_erasure_reference',True)))
        try:
            for name,sql in checks(True).items():connection.execute(text(f'ALTER TABLE audit_actor_reference ADD CONSTRAINT {name} CHECK ({sql})'))
            if _pg_checks(connection,'audit_logs')!=_pg_checks(connection,'pg_temp.audit_actor_reference') or _pg_checks(connection,LEDGER)!=_pg_checks(connection,'pg_temp.audit_erasure_reference'):raise MigrationError('Audit check definition differs')
            ref={c['name']:c for c in inspect(connection).get_columns('audit_erasure_reference',schema=connection.execute(text('SELECT pg_my_temp_schema()::regnamespace::text')).scalar_one())}
            if str(lc[1]['type'])!=str(ref['erased_at']['type']) or normal(lc[1]['default'])!=normal(ref['erased_at']['default']):raise MigrationError('Audit timestamp default/type differs')
        finally:
            connection.execute(text('DROP TABLE audit_actor_reference,audit_erasure_reference'))
        functions=connection.execute(text("SELECT p.prosrc,p.prosecdef,p.proconfig,p.pronargs,p.prorettype='trigger'::regtype,l.lanname,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.pronargdefaults,p.procost,p.prorows,p.prosupport::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname=current_schema() AND p.proname=:n"),{'n':FUNCTION}).all()
        if len(functions)!=1 or tuple(functions[0])!=(PG_BODY,False,['search_path=pg_catalog'],0,True,'plpgsql','v','u',False,False,'f',False,0,100.0,0.0,'-'):raise MigrationError('Audit immutable function differs')
        ts=connection.execute(text("SELECT tgname,tgenabled,tgtype,tgfoid=:f::regproc,tgnargs,tgqual IS NULL,tgattr::text,tgconstraint,tgdeferrable,tginitdeferred,encode(tgargs,'hex') FROM pg_trigger WHERE tgrelid=to_regclass(:t) AND NOT tgisinternal".replace(':f::regproc','CAST(:f AS regproc)')),{'t':LEDGER,'f':FUNCTION}).all()
        if sorted(tuple(r) for r in ts)!=[('audit_actor_erasures_no_change','O',27,True,0,True,'',0,False,False,''),('audit_actor_erasures_no_truncate','O',34,True,0,True,'',0,False,False,'')]:raise MigrationError('Audit immutable triggers differ')
    else:
        if str(lc[1]['type']).upper()!='DATETIME' or normal(lc[1]['default'])!='current_timestamp':raise MigrationError('Audit timestamp differs')
        from sqlalchemy.schema import CreateTable
        from core.models import AuditLog
        # SQLite inspector regex conflates adjacent ALTER-added column CHECKs.
        # Compare exact source-built DDL, not its lossy parsed check list.
        base_sql=str(CreateTable(AuditLog.__table__).compile(dialect=connection.dialect))
        connection.exec_driver_sql(base_sql.replace('CREATE TABLE audit_logs','CREATE TEMP TABLE audit_actor_reference',1))
        try:
            for column,kind,name in zip(ACTOR_COLUMNS,('INTEGER','VARCHAR(128)','VARCHAR(32)'),checks(False)):
                connection.exec_driver_sql(f'ALTER TABLE audit_actor_reference ADD COLUMN {column} {kind} CONSTRAINT {name} CHECK ({checks(False)[name]})')
            reference=connection.execute(text("SELECT sql FROM sqlite_temp_master WHERE name='audit_actor_reference'")).scalar_one().replace('audit_actor_reference','audit_logs',1)
            actual=connection.execute(text("SELECT sql FROM sqlite_master WHERE name='audit_logs'")).scalar_one()
            if normal(actual)!=normal(reference):raise MigrationError('Audit check/original definition differs')
        finally:connection.exec_driver_sql('DROP TABLE audit_actor_reference')
        raw=connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),{'n':LEDGER}).scalar_one()
        if normal(raw)!=normal(ledger_ddl(False)):raise MigrationError('Audit ledger definition differs')
        triggers=dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:n"),{'n':LEDGER}).all())
        if {n:normal(v) for n,v in triggers.items()}!={n:normal(v) for n,v in trigger_sql(False).items()}:raise MigrationError('Audit immutable triggers differ')
    for row in connection.execute(text('SELECT actor_user_id,actor_username,actor_auth_kind FROM audit_logs WHERE actor_auth_kind IS NOT NULL')).all():
        from core.audit_actor import valid_snapshot
        if not valid_snapshot(*row):raise MigrationError('Retained audit actor differs')
    for uid,dt in connection.execute(text(f'SELECT user_id,erased_at FROM {LEDGER}')).all():
        try:
            parsed=dt if isinstance(dt,datetime) else datetime.strptime(dt,'%Y-%m-%d %H:%M:%S')
            if not pg and parsed.strftime('%Y-%m-%d %H:%M:%S')!=dt:raise ValueError()
        except (ValueError,TypeError):raise MigrationError('Retained erasure time differs') from None
    if connection.execute(text(f'SELECT 1 FROM audit_logs a JOIN {LEDGER} e ON a.actor_user_id=e.user_id LIMIT 1')).first():raise MigrationError('Retained erased actor remains')


def apply(connection):
    _boundary(connection)
    if connection.dialect.name not in {'sqlite','postgresql'}:raise MigrationError('Unsupported audit actor dialect')
    pg=connection.dialect.name=='postgresql'
    if pg:connection.execute(text('LOCK TABLE audit_logs IN ACCESS EXCLUSIVE MODE'))
    elif not connection.connection.driver_connection.in_transaction:raise MigrationError('Audit actor migration requires SQLite writer boundary')
    cols={c['name'] for c in inspect(connection).get_columns('audit_logs')}
    footprint=bool(cols&set(ACTOR_COLUMNS)) or inspect(connection).has_table(LEDGER)
    if footprint:
        validate(connection);return
    _base(connection,False)
    if inspect(connection).get_check_constraints('audit_logs'):raise MigrationError('Original audit checks differ')
    if pg:
        if connection.execute(text('SELECT 1 FROM pg_proc WHERE proname=:n'),{'n':FUNCTION}).first():raise MigrationError('Audit function partial footprint')
    elif connection.execute(text("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'audit_actor_erasures_%'" )).first():raise MigrationError('Audit trigger partial footprint')
    definitions=checks(pg)
    for column,kind,check in zip(ACTOR_COLUMNS,('INTEGER','VARCHAR(128)','VARCHAR(32)'),definitions):
        sql=f'ALTER TABLE audit_logs ADD COLUMN {column} {kind}'
        if not pg:sql+=f' CONSTRAINT {check} CHECK ({definitions[check]})'
        connection.execute(text(sql))
    if pg:
        for name,sql in definitions.items():connection.execute(text(f'ALTER TABLE audit_logs ADD CONSTRAINT {name} CHECK ({sql})'))
    connection.execute(text(ledger_ddl(pg)))
    if pg:connection.execute(text(f"CREATE FUNCTION {FUNCTION}() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$"+PG_BODY+'$$'))
    for sql in trigger_sql(pg).values():connection.execute(text(sql))
    validate(connection)
