"""Add independent append-only recovery decisions; no resource cascades."""
import re
import uuid
from sqlalchemy import Column, Integer, String, DateTime, Text, MetaData, Table, CheckConstraint, inspect, text

MIGRATION_ID = 'python:038-dispatch-recovery'
metadata = MetaData()
history = Table('dispatch_recovery_history', metadata,
    Column('id', Integer, primary_key=True),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=text('CURRENT_TIMESTAMP')),
    Column('audit_id', Integer, nullable=False),
    Column('actor_id', Integer, nullable=False),
    Column('actor_identity', String(128), nullable=False),
    Column('owner_org_id', Integer, nullable=False),
    Column('charged_org_id', Integer, nullable=False),
    Column('printer_id', Integer, nullable=False),
    Column('job_id', Integer, nullable=False),
    Column('attempt_handle', String(68), nullable=False),
    Column('snapshot_handle', String(68), nullable=False),
    Column('decision', String(24), nullable=False),
    Column('reason_category', String(48), nullable=False),
    Column('facts', Text, nullable=False),
    CheckConstraint("decision IN ('hold','reconcile-retain','release')", name='ck_recovery_decision'),
    CheckConstraint('actor_id>0 AND owner_org_id>0 AND charged_org_id>0 AND printer_id>0 AND job_id>0', name='ck_recovery_ids'),
    CheckConstraint("length(attempt_handle)=68 AND length(snapshot_handle)=68 AND attempt_handle LIKE 'dr1.%' AND snapshot_handle LIKE 'ds1.%'", name='ck_recovery_handles'),
    CheckConstraint("(decision='hold' AND reason_category IN ('investigation','suspected_duplicate','ownership_review')) OR (decision='reconcile-retain' AND reason_category IN ('investigation','sender_unverified','transport_queue_unverified','status_unavailable','education_authority_unresolved','physical_check_inconclusive')) OR (decision='release' AND reason_category IN ('verified_no_pending_dispatch','verified_terminal_reconciled'))", name='ck_recovery_reason'))


def sqlite_triggers():
    return {'recovery_no_'+op.lower(): f"CREATE TRIGGER recovery_no_{op.lower()} BEFORE {op} ON dispatch_recovery_history BEGIN SELECT RAISE(ABORT, 'immutable recovery history'); END" for op in ('UPDATE','DELETE')}


def pg_functions():
    return {'odin_recovery_immutable': " BEGIN RAISE EXCEPTION 'immutable recovery history'; END; "}


def pg_triggers():
    return {
        (history.name, 'recovery_immutable'): 'CREATE TRIGGER recovery_immutable BEFORE DELETE OR UPDATE ON public.dispatch_recovery_history FOR EACH ROW EXECUTE FUNCTION odin_recovery_immutable()',
        (history.name, 'recovery_no_truncate'): 'CREATE TRIGGER recovery_no_truncate BEFORE TRUNCATE ON public.dispatch_recovery_history FOR EACH STATEMENT EXECUTE FUNCTION odin_recovery_immutable()',
    }


def apply(connection):
    existed = inspect(connection).has_table(history.name)
    if not existed:
        history.create(connection)
        if connection.dialect.name == 'sqlite':
            for definition in sqlite_triggers().values():
                connection.exec_driver_sql(definition)
        elif connection.dialect.name == 'postgresql':
            for name, source in pg_functions().items():
                connection.exec_driver_sql(f'CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $$'+source+'$$')
            for definition in pg_triggers().values():
                connection.exec_driver_sql(definition.replace('EXECUTE FUNCTION odin_recovery_immutable()', 'EXECUTE FUNCTION public.odin_recovery_immutable()'))
        else:
            raise RuntimeError('Unsupported recovery history dialect')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    columns = {c['name']: c for c in inspector.get_columns(history.name)}
    if set(columns) != set(history.c.keys()) or inspector.get_foreign_keys(history.name):
        raise RuntimeError('Recovery history shape differs')
    for name, expected in history.c.items():
        actual = columns[name]
        if actual['nullable'] != expected.nullable or str(actual['type'].compile(dialect=connection.dialect)).upper() != str(expected.type.compile(dialect=connection.dialect)).upper():
            raise RuntimeError('Recovery history column differs')
    if str(columns['created_at'].get('default','')).lower() not in ('current_timestamp','now()'):
        raise RuntimeError('Recovery history server time differs')
    if any(columns[c].get('default') is not None for c in history.c.keys() if c not in ('id','created_at')):
        raise RuntimeError('Recovery history unexpected defaults')
    if inspector.get_unique_constraints(history.name):
        raise RuntimeError('Recovery history unexpected uniqueness')
    if inspector.get_pk_constraint(history.name)['constrained_columns'] != ['id']:
        raise RuntimeError('Recovery history primary key differs')
    def normalized(value):
        return re.sub(r'\s+', '', value).lower()
    name = '_recovery_contract_'+uuid.uuid4().hex[:12]
    expected = history.to_metadata(MetaData(),name=name)
    expected._prefixes = ['TEMPORARY']
    # Dialect-reflected checks preserve PostgreSQL's casts and grouping.
    expected.create(connection)
    try:
        schema = 'temp' if connection.dialect.name=='sqlite' else connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one()
        checks = {c['name']:normalized(c['sqltext']) for c in inspector.get_check_constraints(history.name)}
        wanted = {c['name']:normalized(c['sqltext']) for c in inspect(connection).get_check_constraints(name,schema=schema)}
    finally:
        expected.drop(connection)
    if checks != wanted:
        raise RuntimeError('Recovery history checks differ')
    if connection.dialect.name == 'sqlite':
        triggers = dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='dispatch_recovery_history'")).all())
        for op in ('update','delete'):
            if triggers.get('recovery_no_'+op) != f"CREATE TRIGGER recovery_no_{op} BEFORE {op.upper()} ON dispatch_recovery_history BEGIN SELECT RAISE(ABORT, 'immutable recovery history'); END":
                raise RuntimeError('Recovery history immutability differs')
    else:
        triggers = connection.execute(text("SELECT t.tgname,t.tgtype,t.tgenabled,t.tgqual,t.tgnargs,p.prosrc,p.prosecdef,l.lanname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang WHERE t.tgrelid='dispatch_recovery_history'::regclass AND NOT t.tgisinternal ORDER BY t.tgname")).all()
        expected_triggers = [('recovery_immutable',27),('recovery_no_truncate',34)]
        if len(triggers)!=2:
            raise RuntimeError('Recovery history immutability missing')
        for row, expected_trigger in zip(triggers,expected_triggers):
            if (tuple(row[:2])!=expected_trigger or row[2]!='O' or row[3] is not None or row[4]!=0
                    or row[5].strip()!="BEGIN RAISE EXCEPTION 'immutable recovery history'; END;" or row[6] or row[7]!='plpgsql'):
                raise RuntimeError('Recovery history immutability differs')
