"""Owned minimal actual PostgreSQL027 storage proof; no command producers."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from core import power_target_operation_schema as S
from core.power_target_operations import PowerFacts, PowerStorageError, _stage_operation, _stage_transition, _lookup_replay
from core.schema.migrator import MigrationError, apply_python_migration
import core.schema.migrator as migrator
M=importlib.import_module('core.schema.migrations.027_power_target_operations')
password_path=Path(os.environ['POWER_NATIVE_PASSWORD_FILE'])
assert password_path.stat().st_uid==os.getuid() and stat.S_IMODE(password_path.stat().st_mode)==0o600
print('runtime-private-file uid='+str(os.getuid())+' mode=600')
password=password_path.read_text()
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=password,host='pg',database='postgres'),pool_pre_ping=True)
migrator._ALLOWED_PYTHON_MIGRATIONS |= {'core.schema.migrations.027_power_target_operations'}
checked=[]

def facts(**changes):
    data=dict(printer_id=1,target_key='1'*64,config_key='2'*64,destination_key='3'*64,purpose='manual_toggle',request_nonce='4'*32,actor_user_id=2,auth_kind='bearer_jwt',authority_key='5'*64)
    data.update(changes);return PowerFacts(**data)

def finish(c,o):
    _stage_transition(c,o,0,'preparing','command_started')
    _stage_transition(c,o,1,'command_started','release_pending',outcome='on_observed')
    _stage_transition(c,o,2,'release_pending','complete')

def refusal(action, label, code=None):
    try: action()
    except Exception as error:
        if code is not None: assert getattr(getattr(error,'orig',error),'sqlstate',None)==code,(label,type(error).__name__)
        checked.append(label);return
    raise AssertionError(label+' did not refuse')

# Caller timeout must never be widened, including unit-bearing settings.
for timeout, expected in [('125ms','125ms'),('2s','2s'),('0','2s'),('5s','2s')]:
    with engine.begin() as c:
        S.execute(c, "SELECT set_config('lock_timeout',?,true)", (timeout,))
        S.boundary(c)
        assert S.one(c, 'SHOW lock_timeout')[0] == expected
        checked.append('preserved bounded lock_timeout '+timeout)

# Fresh ordinary actual migration + empty exact adoption, one caller transaction.
with engine.begin() as c: M.apply(c)
with engine.begin() as c: assert apply_python_migration(c,'core.schema.migrations.027_power_target_operations') is True
with engine.begin() as c:
    assert apply_python_migration(c,'core.schema.migrations.027_power_target_operations') is False
    assert len(S.pg_functions())==1 and len(S.pg_triggers())==2
    checked.append('native exact catalog/function/options/two-trigger footprint and empty adoption')

for style in ('connection','session','existing_pg_adapter'):
    if style=='connection':
        c=engine.connect(); tx=c.begin(); cleanup=lambda:(tx.rollback(),c.close())
    elif style=='session':
        c=Session(engine); c.begin(); cleanup=lambda:(c.rollback(),c.close())
    else:
        from core.db_utils import _ConnectionAdapter
        raw=engine.raw_connection(); c=_ConnectionAdapter(raw,True); c.execute('BEGIN'); cleanup=lambda:(c.rollback(),raw.close())
    owner=_stage_operation(c,facts());finish(c,owner)
    assert S.one(c,'SELECT state FROM '+S.TABLE)[0]=='complete'
    cleanup()
    with engine.begin() as other: assert S.one(other,'SELECT 1 FROM '+S.TABLE) is None
    checked.append(style+' actual caller rollback/no helper commit')

with engine.begin() as c: owner=_stage_operation(c,facts())
# Real lock contention during transaction helpers, no favorable lock stand-in.
a=engine.connect();tx=a.begin();M.validate(a)
b=engine.connect();other=b.begin()
try:
    refusal(lambda:S.execute(b,'LOCK TABLE public.'+S.TABLE+' IN ROW EXCLUSIVE MODE NOWAIT'),'native table insertion fence contention','55P03')
finally:other.rollback();b.close();tx.rollback();a.close()

# Original state preserved by every transition rollback.
for destination in ('command_started','release_pending','complete','cleanup_uncertain'):
    c=engine.connect();tx=c.begin()
    if destination=='cleanup_uncertain':_stage_transition(c,owner,0,'preparing',destination,reason='worker_uncertain')
    else:
        _stage_transition(c,owner,0,'preparing','command_started')
        if destination in ('release_pending','complete'):_stage_transition(c,owner,1,'command_started','release_pending',outcome='off_observed')
        if destination=='complete':_stage_transition(c,owner,2,'release_pending','complete')
    tx.rollback();c.close()
    with engine.begin() as c:assert S.one(c,'SELECT state,revision FROM '+S.TABLE)==('preparing',0)
    checked.append(destination+' native rollback')

# Raw immutability/guard/finite negatives, each owned savepoint rolls back.
with engine.begin() as c:
    for statement,label in [("DELETE FROM "+S.TABLE,'delete retained'),('TRUNCATE '+S.TABLE,'truncate retained'),("UPDATE "+S.TABLE+" SET printer_id=2",'immutable original ID'),("UPDATE "+S.TABLE+" SET state='complete',revision=1",'skip transition'),("UPDATE "+S.TABLE+" SET changed_at='infinity'::timestamptz",'infinite present timestamp')]:
        save=c.begin_nested();refusal(lambda:S.execute(c,statement),label,'23514');save.rollback()
    for changed,label in [(facts(request_nonce='a'*32),'active printer uniqueness'),(facts(printer_id=2,request_nonce='a'*32),'active target uniqueness'),(facts(printer_id=2,target_key='a'*64,request_nonce='a'*32),'active destination uniqueness')]:
        save=c.begin_nested();refusal(lambda:_stage_operation(c,changed),label,'23505');save.rollback()
    finish(c,owner)
    assert _lookup_replay(c,facts())=='on_observed'
    refusal(lambda:_lookup_replay(c,facts(purpose='manual_off')),'manual nonce exact purpose')
    refusal(lambda:_lookup_replay(c,facts(authority_key='a'*64)),'manual nonce exact authority')
    M.validate(c)
    checked.append('completed retained replay exact/no command')

# Ordered body, options, disabled/misdirected/extra guard shape are independently rejected.
with engine.begin() as c:
    for statement,label in [("ALTER FUNCTION public."+S.FUNCTION+"() SET search_path TO public",'changed searchpath'),('ALTER TABLE '+S.TABLE+' DISABLE TRIGGER power_target_operations_row_guard','disabled guard'),('DROP TRIGGER power_target_operations_no_truncate ON '+S.TABLE,'missing truncate guard'),('ALTER INDEX ptop_active_target RENAME TO changed_power_target_index','changed exact index name')]:
        save=c.begin_nested();S.execute(c,statement);refusal(lambda:M.validate(c),label);save.rollback()
    save=c.begin_nested();body=S.pg_functions()[S.FUNCTION].replace("IF TG_OP IN ('DELETE','TRUNCATE')", "IF TG_OP IN ('TRUNCATE','DELETE')")
    S.execute(c,'CREATE OR REPLACE FUNCTION public.'+S.FUNCTION+'() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog, public AS $$'+body+'$$')
    refusal(lambda:M.validate(c),'changed exact ordered executable');save.rollback()
    for field in S.TIMES:
        save=c.begin_nested();S.execute(c,'ALTER TABLE '+S.TABLE+' DISABLE TRIGGER power_target_operations_row_guard')
        try: S.execute(c,'UPDATE '+S.TABLE+' SET '+field+"='infinity'::timestamptz")
        except Exception as error:
            assert getattr(getattr(error,'orig',error),'diag').constraint_name=='ptop_finite'
            checked.append('explicit native finite CHECK '+field)
        else: raise AssertionError('finite CHECK accepted '+field)
        save.rollback()
    save=c.begin_nested();S.execute(c,'ALTER TABLE '+S.TABLE+' DROP CONSTRAINT ptop_finite')
    S.execute(c,'ALTER TABLE '+S.TABLE+' DISABLE TRIGGER power_target_operations_row_guard')
    S.execute(c,"UPDATE "+S.TABLE+" SET created_at='infinity',changed_at='infinity',command_started_at='infinity',release_pending_at='infinity',completed_at='infinity'")
    refusal(lambda:S.validate_retained(c),'retained all-equal infinity independent of CHECK/guard');save.rollback()
    save=c.begin_nested();S.execute(c,'ALTER TABLE '+S.TABLE+' DROP CONSTRAINT ptop_finite');S.execute(c,'ALTER TABLE '+S.TABLE+' DISABLE TRIGGER power_target_operations_row_guard')
    S.execute(c,"UPDATE "+S.TABLE+" SET created_at='-infinity'")
    refusal(lambda:S.validate_retained(c),'retained negative infinity independent of CHECK/guard');save.rollback()
    save=c.begin_nested()
    S.execute(c,'ALTER TABLE '+S.TABLE+' DISABLE TRIGGER power_target_operations_row_guard')
    bad="UPDATE "+S.TABLE+" SET state='cleanup_uncertain',completed_at=NULL,cleanup_reason='worker_uncertain',command_started_at=NULL,release_pending_at=NULL,outcome='on_observed'"
    refusal(lambda:S.execute(c,bad),'unreachable uncertain independent CHECK','23514');save.rollback()
    save=c.begin_nested()
    S.execute(c,'ALTER TABLE '+S.TABLE+' DISABLE TRIGGER power_target_operations_row_guard')
    S.execute(c,'ALTER TABLE '+S.TABLE+' DROP CONSTRAINT ptop_state')
    S.execute(c,bad)
    refusal(lambda:S.validate_retained(c),'unreachable uncertain retained independent validation');save.rollback()
    M.validate(c)

# Used unrecorded adoption never creates favorable history; rollback preserves original.
with engine.begin() as c:
    save=c.begin_nested();S.execute(c,'DELETE FROM odin_schema_migrations WHERE migration_id=?',(S.MIGRATION_ID,))
    refusal(lambda:apply_python_migration(c,'core.schema.migrations.027_power_target_operations'),'used unrecorded refusal')
    save.rollback();M.validate(c)

# Empty adoption lock spans helper return to history registration/outer commit.
# Separate owned DB schema reset, no production/source object cleanup.
with engine.begin() as c:
    S.execute(c,'DROP TABLE public.'+S.TABLE);S.execute(c,'DROP FUNCTION public.'+S.FUNCTION+'()')
    S.execute(c,'DELETE FROM odin_schema_migrations WHERE migration_id=?',(S.MIGRATION_ID,));M.apply(c)
a=engine.connect();tx=a.begin();M.apply(a)
b=engine.connect();other=b.begin()
assert S.one(b,'SELECT pg_try_advisory_xact_lock(71403114720480978)')[0] is False
checked.append('existing schema-writer advisory lock through adoption/history')
try:
    refusal(lambda:S.execute(b,'LOCK TABLE public.'+S.TABLE+' IN ROW EXCLUSIVE MODE NOWAIT'),'empty adoption through history insertion fence','55P03')
finally:other.rollback();b.close()
assert apply_python_migration(a,'core.schema.migrations.027_power_target_operations') is True
tx.rollback();a.close()
with engine.begin() as c:
    assert S.one(c,'SELECT 1 FROM odin_schema_migrations WHERE migration_id=?',(S.MIGRATION_ID,)) is None
    assert S.one(c,'SELECT 1 FROM '+S.TABLE) is None
    assert apply_python_migration(c,'core.schema.migrations.027_power_target_operations') is True
    automatic=PowerFacts(printer_id=7,target_key='7'*64,config_key='8'*64,destination_key='9'*64,purpose='cooldown_off',physical_job_id=10,observation_key='a'*64)
    auto=_stage_operation(c,automatic);_stage_transition(c,auto,0,'preparing','release_pending',outcome='not_sent');_stage_transition(c,auto,1,'release_pending','complete')
    # Legitimate numeric reuse uses fresh nonce, preserves old completed history.
    second=_stage_operation(c,automatic)
    assert second.operation_nonce!=auto.operation_nonce
    assert S.one(c,'SELECT count(*) FROM '+S.TABLE)[0]==2
    M.validate(c)
checked.append('adoption rollback and positive automatic/not-sent/numeric reuse retained')
engine.dispose()
print(json.dumps({'scope':'minimal actual PostgreSQL027 storage only; no full bootstrap/backup/commands','checks':checked},indent=2))
print('power-target-storage-native-pass')
