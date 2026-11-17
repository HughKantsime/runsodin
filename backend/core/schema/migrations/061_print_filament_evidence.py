"""Durable per-physical-print filament evidence; no resource cascades or data rewrite."""
import re
import uuid
from sqlalchemy import Column, Integer, SmallInteger, Float, String, DateTime, MetaData, Table, Index, CheckConstraint, inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:061-print-filament-evidence'
metadata = MetaData()
history = Table('print_filament_evidence', metadata,
    Column('id', Integer, primary_key=True),
    Column('print_job_id', Integer, nullable=False),
    Column('print_birth_nonce', String(32), nullable=False, server_default=text("''")),
    Column('kind', String(12), nullable=False),
    Column('claim', SmallInteger),
    Column('source', String(24), nullable=False),
    Column('usage_id', Integer),
    Column('spool_id', Integer),
    Column('grams', Float),
    Column('used_at', DateTime),
    Column('reason', String(40)),
    Column('recorded_at', DateTime, nullable=False, server_default=text('CURRENT_TIMESTAMP')),
    CheckConstraint("kind IN ('usage','closed','unattributed','manual')", name='ck_print_filament_evidence_kind'),
    CheckConstraint('claim IS NULL OR claim = 1', name='ck_print_filament_evidence_claim'))
Index('uq_print_filament_evidence_claim', history.c.print_job_id, history.c.print_birth_nonce,
      unique=True, sqlite_where=text('claim IS NOT NULL'), postgresql_where=text('claim IS NOT NULL'))
Index('ix_print_filament_evidence_print', history.c.print_job_id)
Index('ix_print_filament_evidence_usage', history.c.usage_id)


def sqlite_triggers():
    return {'print_filament_evidence_no_'+op.lower(): f"CREATE TRIGGER print_filament_evidence_no_{op.lower()} BEFORE {op} ON print_filament_evidence BEGIN SELECT RAISE(ABORT, 'immutable print filament evidence'); END" for op in ('UPDATE','DELETE')}


def pg_functions():
    return {'odin_print_filament_evidence_immutable': " BEGIN RAISE EXCEPTION 'immutable print filament evidence'; END; "}


def pg_triggers():
    return {
        (history.name, 'print_filament_evidence_immutable'): 'CREATE TRIGGER print_filament_evidence_immutable BEFORE DELETE OR UPDATE ON public.print_filament_evidence FOR EACH ROW EXECUTE FUNCTION odin_print_filament_evidence_immutable()',
        (history.name, 'print_filament_evidence_no_truncate'): 'CREATE TRIGGER print_filament_evidence_no_truncate BEFORE TRUNCATE ON public.print_filament_evidence FOR EACH STATEMENT EXECUTE FUNCTION odin_print_filament_evidence_immutable()',
    }


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported filament evidence dialect')
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
                connection.exec_driver_sql(definition.replace('EXECUTE FUNCTION odin_print_filament_evidence_immutable()', 'EXECUTE FUNCTION public.odin_print_filament_evidence_immutable()'))
        else:
            raise MigrationError('Unsupported recovery history dialect')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    columns = {c['name']: c for c in inspector.get_columns(history.name)}
    if set(columns) != set(history.c.keys()) or inspector.get_foreign_keys(history.name):
        raise MigrationError('Print filament evidence shape differs')
    for name, expected in history.c.items():
        actual = columns[name]
        wanted_type = 'DOUBLE PRECISION' if name == 'grams' and connection.dialect.name == 'postgresql' else str(expected.type.compile(dialect=connection.dialect)).upper()
        if actual['nullable'] != expected.nullable or str(actual['type'].compile(dialect=connection.dialect)).upper() != wanted_type:
            raise MigrationError('Print filament evidence column differs')
    if str(columns['recorded_at'].get('default','')).lower() not in ('current_timestamp','now()'):
        raise MigrationError('Print filament evidence server time differs')
    if any(columns[c].get('default') is not None for c in history.c.keys() if c not in ('id','recorded_at','print_birth_nonce')):
        raise MigrationError('Print filament evidence unexpected defaults')
    if inspector.get_unique_constraints(history.name):
        raise MigrationError('Print filament evidence unexpected uniqueness')
    if inspector.get_pk_constraint(history.name)['constrained_columns'] != ['id']:
        raise MigrationError('Print filament evidence primary key differs')
    def normalized(value):
        return re.sub(r'\s+', '', value).lower()
    if normalized(columns['print_birth_nonce'].get('default') or '') not in ("''", "''::charactervarying"):
        raise MigrationError('Print filament evidence nonce default differs')
    name = '_filament_contract_'+uuid.uuid4().hex[:12]
    expected = history.to_metadata(MetaData(),name=name)
    expected.indexes.clear()
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
        raise MigrationError('Print filament evidence checks differ')
    indexes = {i['name']: i for i in inspector.get_indexes(history.name)}
    wanted_indexes = {
        'uq_print_filament_evidence_claim': (['print_job_id', 'print_birth_nonce'], True, 'claimisnotnull'),
        'ix_print_filament_evidence_print': (['print_job_id'], False, ''),
        'ix_print_filament_evidence_usage': (['usage_id'], False, ''),
    }
    if set(indexes) != set(wanted_indexes):
        raise MigrationError('Print filament evidence indexes differ')
    for name, (cols, unique, predicate) in wanted_indexes.items():
        actual = indexes[name]
        where = actual.get('dialect_options', {}).get(connection.dialect.name + '_where')
        if (actual['column_names'] != cols or bool(actual['unique']) != unique
                or normalized(str(where) if where is not None else '').strip('()') != predicate):
            raise MigrationError('Print filament evidence index differs')
    if connection.dialect.name == 'sqlite':
        triggers = dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='print_filament_evidence'")).all())
        if set(triggers) != set(sqlite_triggers()):
            raise MigrationError('Print filament evidence triggers differ')
        for op in ('update','delete'):
            if triggers.get('print_filament_evidence_no_'+op) != f"CREATE TRIGGER print_filament_evidence_no_{op} BEFORE {op.upper()} ON print_filament_evidence BEGIN SELECT RAISE(ABORT, 'immutable print filament evidence'); END":
                raise MigrationError('Print filament evidence immutability differs')
    else:
        triggers = connection.execute(text("SELECT t.tgname,t.tgtype,t.tgenabled,t.tgqual,t.tgnargs,p.prosrc,p.prosecdef,l.lanname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang WHERE t.tgrelid='print_filament_evidence'::regclass AND NOT t.tgisinternal ORDER BY t.tgname")).all()
        expected_triggers = [('print_filament_evidence_immutable',27),('print_filament_evidence_no_truncate',34)]
        if len(triggers)!=2:
            raise MigrationError('Print filament evidence immutability missing')
        for row, expected_trigger in zip(triggers,expected_triggers):
            if (tuple(row[:2])!=expected_trigger or row[2]!='O' or row[3] is not None or row[4]!=0
                    or row[5].strip()!="BEGIN RAISE EXCEPTION 'immutable print filament evidence'; END;" or row[6] or row[7]!='plpgsql'):
                raise MigrationError('Print filament evidence immutability differs')
