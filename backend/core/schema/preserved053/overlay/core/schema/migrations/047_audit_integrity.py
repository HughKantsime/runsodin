"""Serialized audit chain, retained genesis, and durable SIEM delivery references."""
import hashlib
import secrets
from pathlib import Path
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError
from core.audit_integrity import GENESIS, hash_expression, install_sqlite, canonical_projection, content_hash
from core.schema.audit_integrity_version import MIGRATION_ID
from core.schema.audit_siem_gaps import gap_sql, chain_gap_sql

COLUMNS = {'prev_hash': 'VARCHAR(64)', 'row_hash': 'VARCHAR(64)', 'chain_seq': 'BIGINT', 'hash_salt': 'VARCHAR(64)'}
TABLES = {
    'audit_siem_gaps': 'gap_id INTEGER PRIMARY KEY NOT NULL, first_id INTEGER NOT NULL, last_id INTEGER NOT NULL, skipped BIGINT NOT NULL, closed INTEGER NOT NULL',
    'audit_redactions': 'row_id INTEGER PRIMARY KEY NOT NULL, record_id INTEGER NOT NULL, evidence TEXT NOT NULL',
    'audit_retention_checkpoints': 'first_seq BIGINT PRIMARY KEY NOT NULL, evidence TEXT NOT NULL',
    'audit_chain_state': 'id INTEGER PRIMARY KEY NOT NULL, genesis VARCHAR(64) NOT NULL, head_hash VARCHAR(64) NOT NULL, head_seq BIGINT NOT NULL, siem_until DOUBLE PRECISION NOT NULL DEFAULT 0',
    'audit_siem_destinations': 'id VARCHAR(32) PRIMARY KEY NOT NULL, name VARCHAR(100) NOT NULL, kind VARCHAR(10) NOT NULL, endpoint TEXT NOT NULL, secret TEXT, enabled INTEGER NOT NULL, tls INTEGER NOT NULL, last_error VARCHAR(100), last_success VARCHAR(32), ca_bundle TEXT, next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, lease_token VARCHAR(32)',
    'audit_siem_outbox': 'destination_id VARCHAR(32) NOT NULL, audit_id INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0, lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, lease_token VARCHAR(32), PRIMARY KEY(destination_id,audit_id)',
    'audit_integrity_status': 'id INTEGER PRIMARY KEY NOT NULL, checked_at DOUBLE PRECISION NOT NULL, result TEXT NOT NULL',
}


SOURCE_PINS = {'schema/audit_siem_gaps.py': '50764a2aae2d795e977e8bdd246a24274a61fde39d60a36502c190cd1dcc34f4', 'audit_integrity.py': '294f574530cb11ec5c4e45c774313aae14a7fef768cd8d4c590efb2f10aab433', 'audit_integrity_legacy.py': 'a0327321fb644972acfe428d47f0f7b14527fde525a08ea26e0e95fbd007ebb8'}


def _source():
    for name, digest in SOURCE_PINS.items():
        if hashlib.sha256((Path(__file__).resolve().parents[2] / name).read_bytes()).hexdigest() != digest:
            raise MigrationError('Audit integrity source contract differs')


def validate_actor_legacy(connection):
    _source()
    from core.audit_integrity_legacy import validate as validate_actor
    validate_actor(connection)


OUTBOX_LIMIT = 10000


def cap_sql(pg):
    # Drop oldest delivery references first. Gap records are chained but never
    # enqueued themselves, preventing recursive queue growth on failed receivers.
    if pg:
        return f"""WITH removed AS (DELETE FROM public.audit_siem_outbox WHERE (destination_id,audit_id) IN
          (SELECT destination_id,audit_id FROM public.audit_siem_outbox ORDER BY audit_id,destination_id
           LIMIT GREATEST(0,(SELECT count(*) FROM public.audit_siem_outbox)+(SELECT count(*) FROM public.audit_siem_destinations WHERE enabled=1)-{OUTBOX_LIMIT})) RETURNING audit_id)
          INSERT INTO public.audit_logs(action,entity_type,details)
          SELECT 'siem.gap','audit_log',json_build_object('dropped',count(*),'first_id',min(audit_id),'last_id',max(audit_id))
          FROM removed HAVING count(*)>0;"""
    candidates = f"SELECT destination_id,audit_id FROM audit_siem_outbox ORDER BY audit_id,destination_id LIMIT max(0,(SELECT count(*) FROM audit_siem_outbox)+(SELECT count(*) FROM audit_siem_destinations WHERE enabled=1 AND (SELECT siem_until FROM audit_chain_state WHERE id=1)>CAST(strftime('%s','now') AS REAL))-{OUTBOX_LIMIT})"
    return f"""{gap_sql(False)}
      INSERT INTO audit_logs(action,entity_type,chain_seq,details)
      SELECT 'siem.gap','audit_log',-1,json_object('dropped',count(*),'first_id',min(audit_id),'last_id',max(audit_id))
      FROM ({candidates}) HAVING count(*)>0 AND NEW.action!='siem.gap';
      {chain_gap_sql()}
      DELETE FROM audit_siem_outbox WHERE NEW.action!='siem.gap' AND (destination_id,audit_id) IN ({candidates});"""


def alert_labels(connection):
    return next(e['labels'] for e in inspect(connection).get_enums(schema='public') if e['name'] == 'alerttype')


def validate_alert_legacy(connection):
    import importlib
    legacy = importlib.import_module('core.schema.migrations.045_pause_cancel_alerts')
    labels = legacy._shape(connection)
    if labels not in (legacy.CURRENT, legacy.CURRENT + ('audit_integrity',)):
        raise MigrationError('Alert enum labels differ')


def pg_functions():
    digest = hash_expression(True, 'NEW.')
    return {'odin_audit_append': f'''DECLARE prior TEXT; seq BIGINT; gap_record INTEGER;
BEGIN
 PERFORM 1 FROM public.audit_chain_state WHERE id=1 FOR UPDATE;
 {gap_sql(True)}
 IF NEW.action != 'siem.gap' AND (SELECT siem_until FROM public.audit_chain_state WHERE id=1)>extract(epoch FROM clock_timestamp()) THEN
 {cap_sql(True)}
 END IF;
 SELECT head_hash,head_seq INTO prior,seq FROM public.audit_chain_state WHERE id=1 FOR UPDATE;
 IF prior IS NULL THEN RAISE EXCEPTION 'audit genesis missing'; END IF;
 NEW.prev_hash := prior; NEW.chain_seq := seq+1;
 NEW.hash_salt := replace(gen_random_uuid()::text||gen_random_uuid()::text,'-','');
 NEW.row_hash := {digest};
 UPDATE public.audit_logs SET prev_hash=NEW.prev_hash,row_hash=NEW.row_hash,chain_seq=NEW.chain_seq,hash_salt=NEW.hash_salt WHERE id=NEW.id;
 UPDATE public.audit_chain_state SET head_hash=NEW.row_hash,head_seq=NEW.chain_seq WHERE id=1;
 INSERT INTO public.audit_siem_outbox(destination_id,audit_id)
 SELECT id,NEW.id FROM public.audit_siem_destinations WHERE enabled=1 AND
 (SELECT siem_until FROM public.audit_chain_state WHERE id=1)>extract(epoch FROM clock_timestamp())
 AND NEW.action != 'siem.gap';
 RETURN NEW;
END'''}


def pg_triggers():
    return {('audit_logs', 'odin_audit_append'): 'CREATE TRIGGER odin_audit_append AFTER INSERT ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION odin_audit_append()'}


def sqlite_trigger():
    digest = hash_expression(False, 'a.')
    return f'''CREATE TRIGGER odin_audit_append AFTER INSERT ON audit_logs WHEN NEW.chain_seq IS NULL BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM audit_chain_state WHERE id=1) THEN RAISE(ABORT,'audit genesis missing') END;
 {cap_sql(False)}
 UPDATE audit_logs SET prev_hash=(SELECT head_hash FROM audit_chain_state WHERE id=1),
 chain_seq=(SELECT head_seq+1 FROM audit_chain_state WHERE id=1),hash_salt=lower(hex(randomblob(32))) WHERE id=NEW.id;
 UPDATE audit_logs SET row_hash=(SELECT {digest} FROM audit_logs a WHERE a.id=NEW.id) WHERE id=NEW.id;
 UPDATE audit_chain_state SET head_hash=(SELECT row_hash FROM audit_logs WHERE id=NEW.id),head_seq=head_seq+1 WHERE id=1;
 INSERT INTO audit_siem_outbox(destination_id,audit_id) SELECT id,NEW.id FROM audit_siem_destinations WHERE enabled=1 AND (SELECT siem_until FROM audit_chain_state WHERE id=1)>CAST(strftime('%s','now') AS REAL) AND NEW.action != 'siem.gap';
END'''


def apply(connection):
    _source()
    pg = connection.dialect.name == 'postgresql'
    if not pg and connection.dialect.name != 'sqlite':
        raise MigrationError('Unsupported audit integrity dialect')
    if pg:
        connection.exec_driver_sql('LOCK TABLE audit_logs IN ACCESS EXCLUSIVE MODE')
        validate_alert_legacy(connection)
        if 'audit_integrity' not in alert_labels(connection):
            connection.exec_driver_sql("ALTER TYPE public.alerttype ADD VALUE 'audit_integrity'")
    else:
        install_sqlite(connection.connection.driver_connection)
    existing = {c['name'] for c in inspect(connection).get_columns('audit_logs')}
    for name, datatype in COLUMNS.items():
        if name not in existing:
            connection.exec_driver_sql(f'ALTER TABLE audit_logs ADD COLUMN {name} {datatype}')
    for name, definition in TABLES.items():
        if not inspect(connection).has_table(name):
            connection.exec_driver_sql(f'CREATE TABLE {name} ({definition})')
    if not connection.execute(text('SELECT 1 FROM audit_chain_state')).first():
        connection.execute(text('INSERT INTO audit_chain_state(id,genesis,head_hash,head_seq) VALUES(1,:g,:g,0)'), {'g': GENESIS})
        previous, seq = GENESIS, 0
        # One-time backfill in the schema transaction, never repeated at startup.
        cursor = connection.execute(text(f'SELECT {canonical_projection(connection)} FROM audit_logs a ORDER BY id')
                                    .execution_options(stream_results=True, max_row_buffer=500)).mappings()
        while batch := cursor.fetchmany(500):
            updates = []
            for record in batch:
                seq += 1
                values = dict(record, prev_hash=previous, hash_salt=secrets.token_hex(32))
                previous = content_hash(values)
                updates.append({'p': values['prev_hash'], 's': seq, 'salt': values['hash_salt'],
                                'h': previous, 'id': values['id']})
            connection.execute(text('UPDATE audit_logs SET prev_hash=:p,chain_seq=:s,hash_salt=:salt,row_hash=:h WHERE id=:id'), updates)
        connection.execute(text('UPDATE audit_chain_state SET head_hash=:h,head_seq=:s WHERE id=1'), {'h': previous, 's': seq})
    if pg:
        for name, body in pg_functions().items():
            connection.exec_driver_sql(f'CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $body${body}$body$')
        if not connection.execute(text("SELECT 1 FROM pg_trigger WHERE tgname='odin_audit_append' AND tgrelid='audit_logs'::regclass")).first():
            connection.exec_driver_sql(next(iter(pg_triggers().values())))
    elif not connection.execute(text("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='odin_audit_append'")).first():
        connection.exec_driver_sql(sqlite_trigger())
    connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_audit_logs_chain_seq ON audit_logs(chain_seq)')
    validate(connection)


def validate(connection):
    _source()
    validate_alert_legacy(connection)
    if connection.dialect.name == 'postgresql' and 'audit_integrity' not in alert_labels(connection):
        raise MigrationError('Audit integrity alert type missing')
    inspector = inspect(connection)
    indexes = inspector.get_indexes('audit_logs')
    if [(i['name'], i['column_names'], bool(i['unique'])) for i in indexes] != [('ix_audit_logs_chain_seq', ['chain_seq'], False)]:
        raise MigrationError('Audit sequence index differs')
    actual = {c['name']: c for c in inspector.get_columns('audit_logs')}
    for name, datatype in COLUMNS.items():
        c = actual.get(name)
        if not c or str(c['type']).upper() != datatype or not c['nullable'] or c['default'] is not None:
            raise MigrationError('Audit integrity column shape differs')
    # Compare source-built reference table shapes, including defaults and keys.
    for name, ddl in TABLES.items():
        ref = 'audit_integrity_reference'
        connection.exec_driver_sql(f'CREATE TEMPORARY TABLE {ref} ({ddl})')
        try:
            inspector = inspect(connection)
            temp_schema = connection.execute(text('SELECT pg_my_temp_schema()::regnamespace::text')).scalar_one() if connection.dialect.name == 'postgresql' else None
            expected = inspector.get_columns(ref, schema=temp_schema)
            columns = inspector.get_columns(name)
            shape = lambda cols: [(c['name'], str(c['type']), c['nullable'], str(c['default'])) for c in cols]
            if shape(columns) != shape(expected) or inspector.get_pk_constraint(name)['constrained_columns'] != inspector.get_pk_constraint(ref, schema=temp_schema)['constrained_columns']:
                raise MigrationError('Audit integrity table shape differs: '+name)
            if inspector.get_foreign_keys(name) or inspector.get_indexes(name) or inspector.get_unique_constraints(name) or inspector.get_check_constraints(name):
                raise MigrationError('Audit integrity constraints differ: '+name)
        finally:
            connection.exec_driver_sql(f'DROP TABLE {ref}')
    if connection.dialect.name == 'sqlite':
        actual = connection.execute(text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='odin_audit_append'")).scalar()
        if actual != sqlite_trigger():
            raise MigrationError('Audit append trigger differs')
    else:
        actual = connection.execute(text("SELECT prosrc,proconfig,prosecdef FROM pg_proc WHERE oid='odin_audit_append()'::regprocedure")).one()
        if tuple(actual) != (pg_functions()['odin_audit_append'], ['search_path=pg_catalog, public'], False):
            raise MigrationError('Audit append function differs')
        trigger = connection.execute(text("SELECT pg_get_triggerdef(oid),tgenabled FROM pg_trigger WHERE tgname='odin_audit_append' AND tgrelid='audit_logs'::regclass")).one()
        if tuple(trigger) != (next(iter(pg_triggers().values())), 'O'):
            raise MigrationError('Audit append trigger differs')
