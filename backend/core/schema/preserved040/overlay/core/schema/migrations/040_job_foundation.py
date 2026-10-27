"""Durable physical terminal observations and bounded job history. No backfill."""
import importlib
import re
import uuid
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:040-job-foundation'
KINDS = ('dispatch_uploading','dispatch_starting','dispatch_dispatched','dispatch_failed','gate_refused',
         'started','paused','resumed','runout','hms','completed','failed','cancelled',
         'bed_clear_confirmed','inspection_recorded','summary_sent')
TABLES = {
 'job_summary_outbox': '''(
 print_attempt_uid TEXT PRIMARY KEY NOT NULL,
 attempt_birth TEXT NOT NULL,
 job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
 printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
 org_id INTEGER,
 outcome TEXT NOT NULL CHECK(outcome IN ('completed','failed','cancelled')),
 observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 delivery_state TEXT NOT NULL DEFAULT '{}',
 UNIQUE(print_attempt_uid,attempt_birth)
)''',
 'stock_snapshots': '''(
 owner_scope TEXT NOT NULL CHECK(owner_scope IN ('global','org')),
 owner_key INTEGER NOT NULL,
 variation TEXT NOT NULL,
 location TEXT NOT NULL,
 quantity TEXT NOT NULL,
 calculated_at TIMESTAMP NOT NULL,
 read_at TIMESTAMP NOT NULL,
 PRIMARY KEY(owner_scope,owner_key,variation,location)
)''',
 'job_timeline_events': '''(
 id TEXT PRIMARY KEY NOT NULL,
 job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 print_attempt_uid TEXT,
 printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
 org_id INTEGER,
 kind TEXT NOT NULL CHECK(kind IN ('''+','.join("'"+k+"'" for k in KINDS)+''')),
 code TEXT,
 detail TEXT NOT NULL DEFAULT '{}',
 actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 actor_token_id INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
 occurred_at TIMESTAMP NOT NULL,
 recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK(kind='bed_clear_confirmed' OR (actor_user_id IS NULL AND actor_token_id IS NULL))
)''',
 'timeline_writer_state': '''(
 process_id TEXT PRIMARY KEY NOT NULL,
 started_at TIMESTAMP NOT NULL,
 clean_shutdown INTEGER NOT NULL DEFAULT 0 CHECK(clean_shutdown IN (0,1)),
 last_drained_at TIMESTAMP,
 drop_epoch INTEGER NOT NULL DEFAULT 0,
 error_epoch INTEGER NOT NULL DEFAULT 0,
 printer_ids TEXT NOT NULL DEFAULT '[]',
 epoch_history TEXT NOT NULL DEFAULT '[]'
)'''
}
INDEXES = {
 'ix_job_timeline_job_time': 'CREATE INDEX ix_job_timeline_job_time ON job_timeline_events (job_id, occurred_at)',
 'ix_job_timeline_attempt': 'CREATE INDEX ix_job_timeline_attempt ON job_timeline_events (print_attempt_uid)'
}


def _mutation_condition(postgres=False):
    same=' IS NOT DISTINCT FROM ' if postgres else ' IS '
    # Only erasure of an existing actor or FK printer nulling can change a row.
    fixed=('id','job_id','print_attempt_uid','org_id','kind','code','detail','occurred_at','recorded_at')
    terms=['NEW.'+c+same+'OLD.'+c for c in fixed]
    terms += ['(NEW.'+c+same+'OLD.'+c+' OR NEW.'+c+' IS NULL)' for c in ('actor_user_id','actor_token_id','printer_id')]
    terms.append('('+' OR '.join('(OLD.'+c+' IS NOT NULL AND NEW.'+c+' IS NULL)' for c in ('actor_user_id','actor_token_id','printer_id'))+')')
    return ' AND '.join(terms)


def sqlite_triggers():
    return {'timeline_mutation_guard': "CREATE TRIGGER timeline_mutation_guard BEFORE UPDATE ON job_timeline_events WHEN NOT ("+_mutation_condition()+") BEGIN SELECT RAISE(ABORT,'immutable job timeline'); END"}


def pg_functions():
    return {'odin_timeline_mutation_guard': "BEGIN IF NOT ("+_mutation_condition(True)+") THEN RAISE EXCEPTION 'immutable job timeline'; END IF; RETURN NEW; END;"}


def pg_triggers():
    return {('job_timeline_events','timeline_mutation_guard'): 'CREATE TRIGGER timeline_mutation_guard BEFORE UPDATE ON public.job_timeline_events FOR EACH ROW EXECUTE FUNCTION public.odin_timeline_mutation_guard()'}


def apply(connection):
    present=[inspect(connection).has_table(t) for t in TABLES]
    if any(present):
        if not all(present):raise MigrationError('Partial job foundation schema')
        validate(connection);return
    if connection.dialect.name not in ('sqlite','postgresql'):raise MigrationError('Unsupported job foundation dialect')
    for table,ddl in TABLES.items():connection.exec_driver_sql('CREATE TABLE '+table+' '+ddl)
    for ddl in INDEXES.values():connection.exec_driver_sql(ddl)
    if connection.dialect.name=='sqlite':
        for ddl in sqlite_triggers().values():connection.exec_driver_sql(ddl)
    else:
        for name,body in pg_functions().items():
            connection.exec_driver_sql(f'CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog, public AS $${body}$$')
        for ddl in pg_triggers().values():connection.exec_driver_sql(ddl)
    validate(connection)


def validate(connection):
    shape=importlib.import_module('core.schema.migrations.039_measured_filament')._shape
    postgres=connection.dialect.name=='postgresql'
    for table,ddl in TABLES.items():
        name='_job_contract_'+uuid.uuid4().hex[:12]
        reference=r' REFERENCES (\w+)\((\w+)\) ON DELETE (CASCADE|SET NULL)'
        expected_ddl=re.sub(reference,'',ddl) if postgres else ddl
        connection.exec_driver_sql(('CREATE TEMPORARY TABLE ' if postgres else 'CREATE TABLE ')+name+' '+expected_ddl)
        try:
            schema=connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one() if postgres else None  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
            wanted=shape(connection,name,schema)
            if postgres:
                wanted['foreign']=sorted(((column,),target,(key,),str({'ondelete':action}))
                    for column,target,key,action in re.findall(r'(\w+) INTEGER(?: NOT NULL)?'+reference,ddl))
            actual=shape(connection,table)
            indexes=actual.pop('indexes');wanted.pop('indexes')
            expected_indexes=[(('job_id','occurred_at'),0),(('print_attempt_uid',),0)] if table=='job_timeline_events' else []
            if actual!=wanted or sorted(indexes)!=expected_indexes:raise MigrationError('Job foundation shape differs: '+table)
        finally:connection.exec_driver_sql('DROP TABLE '+name)
    if not postgres:
        actual=dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='job_timeline_events'")).all())  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
        if actual!=sqlite_triggers():raise MigrationError('Job timeline guard differs')
    else:
        rows=connection.execute(text("SELECT t.tgname,t.tgtype,t.tgenabled,t.tgqual,t.tgnargs,p.proname,p.prosrc,p.proconfig,p.prosecdef,l.lanname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_language l ON l.oid=p.prolang WHERE t.tgrelid='job_timeline_events'::regclass AND NOT t.tgisinternal ORDER BY t.tgname")).all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL; values are bound
        expected=[('timeline_mutation_guard',19,'O',None,0,'odin_timeline_mutation_guard',pg_functions()['odin_timeline_mutation_guard'],['search_path=pg_catalog, public'],False,'plpgsql')]
        if [tuple(r) for r in rows]!=expected:raise MigrationError('Job timeline guard differs')
