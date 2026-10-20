"""Strict additive AMS evidence adoption. Unregistered until native acceptance."""
import hashlib
import re
import uuid
from pathlib import Path

from sqlalchemy import (CheckConstraint, Column, MetaData, Table,
                        UniqueConstraint, inspect, text)
from sqlalchemy.schema import CreateTable
from sqlalchemy.exc import DBAPIError
from core.ams_evidence_schema import EVIDENCE_TABLES, packets
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:020-ams-evidence'
SCHEMA_SOURCE_SHA256 = '9b4b919c0756a7a032b5d13c6cab250fda2cf614d43fd2a05add78f030db0df1'


def _source():
    path = Path(__file__).resolve().parents[2] / 'ams_evidence_schema.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != SCHEMA_SOURCE_SHA256:
        raise MigrationError('AMS source contract changed')


def _normalize(value):
    parts = re.split(r"('(?:''|[^'])*')", str(value))
    return ''.join(part if i % 2 else re.sub(r'\s+', '', part).lower()
                   for i, part in enumerate(parts))


def _checks(connection, table, *, roundtrip=False, raw_output=False):
    if roundtrip and connection.dialect.name != 'postgresql':
        raise MigrationError('AMS native roundtrip unavailable')
    raw = None
    if roundtrip:
        raw = _checks(connection,table,raw_output=True)
    name = '_odin_ams_contract_' + uuid.uuid4().hex[:12]
    template = Table(name, MetaData(),
        *(Column(c.name, c.type, nullable=c.nullable) for c in table.columns),
        *(CheckConstraint(raw[c.name] if raw is not None else str(c.sqltext), name=name+'_'+c.name)
          for c in table.constraints if isinstance(c, CheckConstraint)),
        prefixes=['TEMPORARY'])
    template.create(connection)
    try:
        schema = 'temp' if connection.dialect.name == 'sqlite' else connection.execute(
            text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one()
        return {c['name'].removeprefix(name+'_'):(c['sqltext'] if raw_output else _normalize(c['sqltext']))
                for c in inspect(connection).get_check_constraints(name, schema=schema)}
    finally:
        template.drop(connection)


def _table(connection, table):
    inspector = inspect(connection)
    actual = inspector.get_columns(table.name)
    if {c['name'] for c in actual} != set(table.columns.keys()):
        raise MigrationError('AMS columns differ')
    for c in actual:
        expected = table.columns[c['name']]
        if (str(c['type'].compile(dialect=connection.dialect)).upper() !=
                str(expected.type.compile(dialect=connection.dialect)).upper()
                or c['nullable'] != expected.nullable or c.get('default') is not None):
            raise MigrationError('AMS column shape differs')
        if bool(c.get('identity')) != (table is packets and c['name']=='packet_sequence'
                                      and connection.dialect.name=='postgresql'):
            raise MigrationError('AMS identity differs')
    if inspector.get_pk_constraint(table.name)['constrained_columns'] != list(table.primary_key.columns.keys()):
        raise MigrationError('AMS primary key differs')
    expected_unique = {tuple(c.columns.keys()) for c in table.constraints if isinstance(c, UniqueConstraint)}
    actual_unique = {tuple(c['column_names']) for c in inspector.get_unique_constraints(table.name)}
    for index in inspector.get_indexes(table.name):
        if not index.get('duplicates_constraint'):
            raise MigrationError('AMS unexpected index')
    if actual_unique != expected_unique:
        raise MigrationError('AMS uniqueness differs')
    def fk_shape(c):
        options = c.get('options', {})
        if (c.get('referred_schema') not in (None, inspector.default_schema_name)
                or options.get('deferrable') or options.get('initially')
                or options.get('onupdate') not in (None,'NO ACTION')):
            raise MigrationError('AMS foreign authority differs')
        return (tuple(c['constrained_columns']), c['referred_table'],
                tuple(c['referred_columns']), options.get('ondelete') or 'NO ACTION')
    expected_fk = {(tuple(c.column_keys), list(c.elements)[0].column.table.name,
                    tuple(e.column.name for e in c.elements), c.ondelete or 'NO ACTION')
                   for c in table.foreign_key_constraints}
    foreign = inspector.get_foreign_keys(table.name)
    if len(foreign)!=len(expected_fk) or {fk_shape(c) for c in foreign} != expected_fk:
        raise MigrationError('AMS foreign keys differ')
    checks = {c['name']:_normalize(c['sqltext']) for c in inspector.get_check_constraints(table.name)}
    expected_checks = _checks(connection, table)
    if checks != expected_checks:
        if connection.dialect.name != 'postgresql':
            raise MigrationError('AMS checks differ')
        restored_checks = _checks(connection,table,roundtrip=True)
        if (set(checks) != set(expected_checks) or any(
                checks[name] not in (expected_checks[name],restored_checks[name])
                for name in checks)):
            raise MigrationError('AMS checks differ')
    if connection.dialect.name == 'sqlite':
        # Strict canonical adoption also rejects rowid/sequence behavior drift.
        ddl = connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
                                 {'name':table.name}).scalar_one()
        if _normalize(ddl) != _normalize(str(CreateTable(table).compile(dialect=connection.dialect))):
            raise MigrationError('AMS SQLite creation contract differs')
        if (connection.exec_driver_sql("PRAGMA integrity_check('%s')" % table.name).scalars().all()!=['ok']
                or connection.exec_driver_sql("PRAGMA foreign_key_check('%s')" % table.name).first() is not None):
            raise MigrationError('AMS retained records violate constraints')
        if table is packets:
            last = connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name=:name"),
                                      {'name':table.name}).scalar_one_or_none()
            maximum = connection.execute(text('SELECT max(packet_sequence) FROM ams_report_packets')).scalar_one()
            if maximum is not None and (last is None or last < maximum):
                raise MigrationError('AMS SQLite sequence position differs')
    else:
        authority = connection.execute(text("""SELECT c.convalidated,c.condeferrable,
            c.contype<>'f' OR p.relnamespace=t.relnamespace
            FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
            LEFT JOIN pg_class p ON p.oid=c.confrelid
            WHERE c.conrelid=to_regclass(:name) AND c.contype IN ('c','f')"""),
            {'name':table.name}).all()
        if any(row != (True,False,True) for row in authority):
            raise MigrationError('AMS native constraints differ')
        keys = connection.execute(text("""SELECT c.convalidated,c.condeferrable,
            i.indisvalid,i.indisready,i.indimmediate,i.indnullsnotdistinct,
            i.indpred IS NULL,i.indexprs IS NULL,i.indnkeyatts=i.indnatts
            FROM pg_constraint c JOIN pg_index i ON i.indexrelid=c.conindid
            WHERE c.conrelid=to_regclass(:name) AND c.contype IN ('p','u')"""),
            {'name':table.name}).all()
        if (len(keys) != 1+len(expected_unique)
                or any(tuple(row)!=(True,False,True,True,True,False,True,True,True) for row in keys)):
            raise MigrationError('AMS native key enforcement differs')
        if table is packets:
            row = connection.execute(text("""SELECT a.attidentity,s.seqcycle,s.seqincrement,
                s.seqmin,s.seqstart,s.seqmax,s.seqcache
                FROM pg_attribute a JOIN pg_sequence s
                ON s.seqrelid=pg_get_serial_sequence(:name,'packet_sequence')::regclass
                WHERE a.attrelid=to_regclass(:name) AND a.attname='packet_sequence'"""),
                {'name':table.name}).one()
            if tuple(row) != ('a',False,1,1,1,2147483647,1):
                raise MigrationError('AMS native sequence differs')
            sequence = connection.execute(text("""SELECT n.nspname,s.relname,
                s.relnamespace=t.relnamespace,s.relkind FROM pg_class s
                JOIN pg_namespace n ON n.oid=s.relnamespace
                JOIN pg_class t ON t.oid=to_regclass(:name)
                WHERE s.oid=pg_get_serial_sequence(:name,'packet_sequence')::regclass"""),
                {'name':table.name}).one()
            if sequence[2:] != (True,'S'):
                raise MigrationError('AMS native sequence authority differs')
            quote = connection.dialect.identifier_preparer
            position = connection.exec_driver_sql('SELECT last_value,is_called FROM %s.%s' %
                (quote.quote_schema(sequence[0]),quote.quote(sequence[1]))).one()
            maximum = connection.execute(text('SELECT max(packet_sequence) FROM ams_report_packets')).scalar_one()
            next_value = position[0]+1 if position[1] else position[0]
            if next_value>2147483647 or (maximum is not None and next_value<=maximum):
                raise MigrationError('AMS native sequence position differs')


def _retained(connection):
    for query in (
        """SELECT 1 FROM ams_current_reads c LEFT JOIN printers p ON p.id=c.printer_id
           LEFT JOIN ams_read_generations g ON g.generation_id=c.generation_id
           AND g.printer_id=c.printer_id AND g.runtime_incarnation=c.runtime_incarnation
           WHERE p.id IS NULL OR g.generation_id IS NULL LIMIT 1""",
        """SELECT 1 FROM ams_report_packets p LEFT JOIN ams_read_generations g
           ON g.generation_id=p.generation_id WHERE g.generation_id IS NULL
           OR NOT EXISTS(SELECT 1 FROM ams_report_trays t
                         WHERE t.packet_sequence=p.packet_sequence) LIMIT 1""",
        """SELECT 1 FROM ams_report_trays t LEFT JOIN ams_report_packets p
           ON p.packet_sequence=t.packet_sequence WHERE p.packet_sequence IS NULL LIMIT 1""",
        """SELECT 1 FROM ams_read_generations g WHERE g.state='ready' AND
           NOT EXISTS(SELECT 1 FROM ams_report_packets p
                      WHERE p.generation_id=g.generation_id) LIMIT 1""",
    ):
        if connection.execute(text(query)).first():
            raise MigrationError('AMS retained evidence relationship differs')
    invalid = connection.execute(text("""SELECT 1 FROM ams_current_reads c
        JOIN ams_read_generations g ON g.generation_id=c.generation_id
        WHERE g.state NOT IN ('acquiring','ready') LIMIT 1""")).first()
    if invalid:
        raise MigrationError('AMS current pointer is terminal')
    invalid = connection.execute(text("""SELECT 1 FROM ams_report_packets p
        JOIN ams_read_generations g ON g.generation_id=p.generation_id
        WHERE g.state NOT IN ('ready','invalidated') OR p.received_at<g.started_at
        OR p.received_at>g.completed_at LIMIT 1""")).first()
    if invalid:
        raise MigrationError('AMS packet generation chronology differs')


def _boundary(connection):
    if connection.dialect.name == 'sqlite':
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        # Promote an existing deferred transaction to writer authority, without
        # modifying a live row or committing the caller-owned transaction.
        connection.execute(text('UPDATE printers SET id=id WHERE 0'))
    elif connection.dialect.name == 'postgresql':
        if connection.execute(text('SHOW transaction_isolation')).scalar_one() != 'read committed':
            raise MigrationError('AMS validation requires fresh locked snapshots')
        schema = connection.execute(text('SELECT current_schema()')).scalar_one()
        if not schema:
            raise MigrationError('AMS validation schema unavailable')
        preparer = connection.dialect.identifier_preparer
        names = sorted(['printers'] + [t.name for t in EVIDENCE_TABLES])
        for name in names:
            row = connection.execute(text("""SELECT c.oid,c.relkind,
                c.oid=to_regclass(:name) FROM pg_class c JOIN pg_namespace n
                ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname=:name"""),
                {'name':name,'schema':schema}).first()
            if row is None:
                if name=='printers':
                    raise MigrationError('AMS live printer authority missing')
                continue
            if row[1:] != ('r', True):
                raise MigrationError('AMS native table authority differs')
            try:
                connection.exec_driver_sql('LOCK TABLE %s.%s IN SHARE ROW EXCLUSIVE MODE NOWAIT' %
                    (preparer.quote_schema(schema), preparer.quote(name)))
            except DBAPIError as error:
                if getattr(error.orig,'sqlstate',None)=='55P03' or getattr(error.orig,'pgcode',None)=='55P03':
                    raise MigrationError('AMS validation authority busy') from None
                raise
    else:
        raise MigrationError('AMS database dialect unavailable')


def validate(connection):
    _source()
    _boundary(connection)
    for table in EVIDENCE_TABLES:
        if not inspect(connection).has_table(table.name):
            raise MigrationError('AMS table missing')
        _table(connection, table)
    _retained(connection)


def apply(connection):
    _source()
    _boundary(connection)
    for table in EVIDENCE_TABLES:
        if inspect(connection).has_table(table.name):
            _table(connection, table)
    for table in EVIDENCE_TABLES:
        table.create(connection, checkfirst=True)
    validate(connection)
