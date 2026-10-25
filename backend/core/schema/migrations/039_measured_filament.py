"""Retained terminal measurements and append-only valuation revisions. No backfill.

References target retained anchors, never deletable live resources. INSERT guards
also reject SQLite REPLACE and enforce the complete owner/order/item graph.
"""
import re
import uuid
from sqlalchemy import text, inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:039-measured-filament'
TABLES = {
 'terminal_filament_measurements': '''(
 observation_id TEXT PRIMARY KEY NOT NULL,
 attempt_uid TEXT NOT NULL REFERENCES cost_attempt_bindings(attempt_uid) ON DELETE RESTRICT,
 attempt_birth TEXT NOT NULL REFERENCES cost_source_lifetimes(birth_nonce) ON DELETE RESTRICT,
 owner_scope TEXT NOT NULL, owner_key INTEGER NOT NULL,
 order_uid TEXT NOT NULL REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT,
 item_uid TEXT NOT NULL REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT,
 allocation_key TEXT NOT NULL CHECK(allocation_key='attempt-total'),
 outcome TEXT NOT NULL CHECK(outcome IN ('completed','failed','cancelled')),
 measurement_basis TEXT NOT NULL CHECK(measurement_basis IN ('printer_reported','unavailable','invalid')),
 measured_grams TEXT, reported_value TEXT CHECK(reported_value IS NULL OR length(reported_value)<=128),
 reconciliation_reason TEXT CHECK(reconciliation_reason IS NULL OR reconciliation_reason='inventory_shortfall'),
 snapshot_json TEXT NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(attempt_uid,allocation_key),
 CHECK((owner_scope='global' AND owner_key=0) OR (owner_scope='org' AND owner_key>0)),
 CHECK(length(observation_id)=32 AND length(attempt_uid)=36 AND length(attempt_birth)=32),
 CHECK(length(snapshot_json)<=65536),
 CHECK((measurement_basis='printer_reported' AND measured_grams IS NOT NULL AND length(measured_grams)<=40) OR
       (measurement_basis IN ('unavailable','invalid') AND measured_grams IS NULL))
)''',
 'filament_cost_events': '''(
 event_id TEXT PRIMARY KEY NOT NULL,
 observation_id TEXT NOT NULL REFERENCES terminal_filament_measurements(observation_id) ON DELETE RESTRICT,
 revision INTEGER NOT NULL CHECK(revision>0 AND revision<=2147483647),
 expected_revision INTEGER NOT NULL CHECK(expected_revision=revision-1),
 audit_reason TEXT NOT NULL CHECK(length(audit_reason)>0 AND length(audit_reason)<=200),
 spool_uid TEXT REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT,
 purchase_evidence_nonce TEXT REFERENCES spool_purchase_evidence(evidence_nonce) ON DELETE RESTRICT,
 currency TEXT CHECK(currency IN ('USD','EUR','GBP','CAD','AUD','JPY','NZD','CHF')),
 amount_decimal TEXT,
 reason TEXT CHECK(reason IN ('missing_measurement','invalid_measurement','missing_historical_valuation','unresolved_allocation','unresolved_purchase_evidence','currency_mismatch','valuation_bound_exceeded','mixed_order_plate','archive_ownership_unresolved')),
 calculation_version INTEGER NOT NULL CHECK(calculation_version=1),
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(observation_id,revision),
 CHECK(length(event_id)=32),
 CHECK((reason IS NULL AND currency IS NOT NULL AND amount_decimal IS NOT NULL AND spool_uid IS NOT NULL AND purchase_evidence_nonce IS NOT NULL AND length(amount_decimal)<=100) OR (reason IS NOT NULL AND amount_decimal IS NULL))
)'''
}


def conditions():
    return {
 'terminal_filament_measurements': """EXISTS(SELECT 1 FROM terminal_filament_measurements x WHERE x.observation_id=NEW.observation_id OR (x.attempt_uid=NEW.attempt_uid AND x.allocation_key=NEW.allocation_key)) OR NOT EXISTS(
 SELECT 1 FROM cost_attempt_bindings b JOIN cost_source_lifetimes l ON l.birth_nonce=NEW.attempt_birth
 WHERE b.attempt_uid=NEW.attempt_uid AND b.order_uid=NEW.order_uid AND b.item_uid=NEW.item_uid
 AND b.owner_scope=NEW.owner_scope AND b.owner_key=NEW.owner_key
 AND l.resource_kind='attempt' AND l.state='known' AND l.identity_uid=b.attempt_uid AND l.owner_scope=b.owner_scope AND l.owner_key=b.owner_key)""",
 'filament_cost_events': """EXISTS(SELECT 1 FROM filament_cost_events x WHERE x.event_id=NEW.event_id OR (x.observation_id=NEW.observation_id AND x.revision=NEW.revision)) OR NEW.expected_revision<>COALESCE((SELECT MAX(revision) FROM filament_cost_events WHERE observation_id=NEW.observation_id),0)
 OR (NEW.spool_uid IS NOT NULL AND NOT EXISTS(SELECT 1 FROM terminal_filament_measurements m JOIN cost_identities s ON s.identity_uid=NEW.spool_uid WHERE m.observation_id=NEW.observation_id AND s.resource_kind='spool' AND s.owner_scope=m.owner_scope AND s.owner_key=m.owner_key))
 OR (NEW.purchase_evidence_nonce IS NOT NULL AND NOT EXISTS(SELECT 1 FROM spool_purchase_evidence e JOIN terminal_filament_measurements m ON m.observation_id=NEW.observation_id WHERE e.evidence_nonce=NEW.purchase_evidence_nonce AND e.spool_uid=NEW.spool_uid AND e.owner_scope=m.owner_scope AND e.owner_key=m.owner_key AND e.currency=NEW.currency))"""
    }


def sqlite_triggers():
    result={}
    for table,condition in conditions().items():
        for op in ('UPDATE','DELETE'):
            name=table+'_no_'+op.lower()
            result[name]=f"CREATE TRIGGER {name} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable measured filament evidence'); END"
        name=table+'_insert_guard'
        result[name]=f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'measured filament reference or revision differs'); END"
    return result


def pg_functions():
    return {table+'_guard': "BEGIN IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'immutable measured filament evidence'; END IF; "
            + ("PERFORM 1 FROM terminal_filament_measurements WHERE observation_id=NEW.observation_id FOR UPDATE; " if table=='filament_cost_events' else '')
            + f"IF {condition} THEN RAISE EXCEPTION 'measured filament reference or revision differs'; END IF; RETURN NEW; END;"
            for table,condition in conditions().items()}


def pg_triggers():
    result={}
    for table in TABLES:
        for suffix,ops,level in [('guard','INSERT OR DELETE OR UPDATE','ROW'),('no_truncate','TRUNCATE','STATEMENT')]:
            name=table+'_'+suffix
            result[(table,name)]=f'CREATE TRIGGER {name} BEFORE {ops} ON public.{table} FOR EACH {level} EXECUTE FUNCTION {table}_guard()'
    return result


def apply(connection):
    present=[inspect(connection).has_table(t) for t in TABLES]
    if any(present):
        if not all(present):raise MigrationError('Partial measured filament schema')
        validate(connection);return
    if connection.dialect.name not in ('sqlite','postgresql'):raise MigrationError('Unsupported measured filament dialect')
    for table,ddl in TABLES.items():connection.exec_driver_sql('CREATE TABLE '+table+' '+ddl)
    if connection.dialect.name=='sqlite':
        for ddl in sqlite_triggers().values():connection.exec_driver_sql(ddl)
    else:
        for name,body in pg_functions().items():
            connection.exec_driver_sql(f'CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog, public AS $${body}$$')
        for ddl in pg_triggers().values():connection.exec_driver_sql(ddl)
    validate(connection)


def _shape(connection,table):
    i=inspect(connection)
    return {
      'columns':[(c['name'],str(c['type']),c['nullable'],str(c.get('default'))) for c in i.get_columns(table)],
      'pk':i.get_pk_constraint(table)['constrained_columns'],
      'unique':sorted(tuple(c['column_names']) for c in i.get_unique_constraints(table)),
      'checks':sorted(re.sub(r'\s+','',c['sqltext']).lower() for c in i.get_check_constraints(table)),
      'foreign':sorted((tuple(c['constrained_columns']),c['referred_table'],tuple(c['referred_columns']),str(c['options'])) for c in i.get_foreign_keys(table)),
      'indexes':sorted((tuple(c['column_names']),c['unique']) for c in i.get_indexes(table) if not c.get('duplicates_constraint'))
    }


def validate(connection):
    # Reflect dialect-native expected declarations; adoption requires exact constraints.
    for table,ddl in TABLES.items():
        name='_measured_contract_'+uuid.uuid4().hex[:12]
        connection.exec_driver_sql('CREATE TABLE '+name+' '+ddl)
        try:
            if _shape(connection,table)!=_shape(connection,name):raise MigrationError('Measured filament shape differs')
        finally:connection.exec_driver_sql('DROP TABLE '+name)
    # Adoption must validate retained rows as well as the guards protecting
    # future writes. Reinstalled guards do not prove pre-existing associations.
    terminal_bad='NOT EXISTS('+conditions()['terminal_filament_measurements'].split(' OR NOT EXISTS(',1)[1]
    event_bad='NEW.expected_revision'+conditions()['filament_cost_events'].split(' OR NEW.expected_revision',1)[1]
    event_bad=event_bad.replace('WHERE observation_id=NEW.observation_id),0)', 'WHERE observation_id=NEW.observation_id AND revision<NEW.revision),0)')
    event_bad+=' OR NOT EXISTS(SELECT 1 FROM terminal_filament_measurements m WHERE m.observation_id=NEW.observation_id)'
    for table,condition in [('terminal_filament_measurements',terminal_bad),('filament_cost_events',event_bad)]:
        if connection.execute(text('SELECT 1 FROM '+table+' AS source WHERE '+condition.replace('NEW.','source.')+' LIMIT 1')).first():
            raise MigrationError('Measured filament retained association or revision differs')
    if connection.execute(text('SELECT 1 FROM terminal_filament_measurements m WHERE NOT EXISTS(SELECT 1 FROM filament_cost_events e WHERE e.observation_id=m.observation_id AND e.revision=1) LIMIT 1')).first():
        raise MigrationError('Measured filament original event missing')
    if connection.dialect.name=='sqlite':
        actual=dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('terminal_filament_measurements','filament_cost_events')")).all())
        if actual!=sqlite_triggers():raise MigrationError('Measured filament guards differ')
        for table in TABLES:
            if connection.exec_driver_sql('PRAGMA foreign_key_check('+table+')').first():raise MigrationError('Measured filament retained reference differs')
    else:
        rows=connection.execute(text("""SELECT t.tgname,t.tgtype,t.tgenabled,t.tgqual,t.tgnargs,p.proname,p.prosrc,p.proconfig,p.prosecdef,l.lanname
         FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang
         WHERE t.tgrelid IN ('terminal_filament_measurements'::regclass,'filament_cost_events'::regclass) AND NOT t.tgisinternal ORDER BY t.tgname""")).all()
        expected=[]
        for table in TABLES:
            for suffix,kind in [('guard',31),('no_truncate',34)]:
                expected.append((table+'_'+suffix,kind,'O',None,0,table+'_guard',pg_functions()[table+'_guard'],['search_path=pg_catalog, public'],False,'plpgsql'))
        if [tuple(r) for r in rows]!=sorted(expected):raise MigrationError('Measured filament guards differ')
