"""027 storage-only receipts. No power/camera/printer commands or authority claims."""
import hashlib
import importlib
from pathlib import Path
import sqlite3
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from core import power_target_operation_schema as S
from core.power_target_operations import PowerFacts, PowerStorageError, _stage_operation, _stage_transition, _lookup_replay
from core.schema.migrator import MigrationError, ensure_ledger, apply_python_migration
import core.schema.migrator as migrator
M = importlib.import_module('core.schema.migrations.027_power_target_operations')


def engine_for(path=':memory:'):
    engine=create_engine('sqlite:///'+str(path))
    @event.listens_for(engine,'connect')
    def pragmas(c,_): c.execute('PRAGMA foreign_keys=ON')
    return engine


@pytest.fixture
def power_engine(monkeypatch):
    # Only this owned isolated fixture admits the new migration; shared runtime
    # registration stays untouched until root's separate integration batch.
    monkeypatch.setattr(migrator,'_ALLOWED_PYTHON_MIGRATIONS',migrator._ALLOWED_PYTHON_MIGRATIONS|{'core.schema.migrations.027_power_target_operations'})
    engine=engine_for()
    with engine.begin() as c: apply_python_migration(c,'core.schema.migrations.027_power_target_operations')
    yield engine
    engine.dispose()


def manual(**changes):
    values=dict(printer_id=1,target_key='1'*64,config_key='2'*64,destination_key='3'*64,purpose='manual_toggle',request_nonce='4'*32,actor_user_id=2,auth_kind='bearer_jwt',authority_key='5'*64)
    values.update(changes); return PowerFacts(**values)


def automatic(**changes):
    values=dict(printer_id=1,target_key='1'*64,config_key='2'*64,destination_key='3'*64,purpose='observed_on',physical_job_id=6,observation_key='7'*64)
    values.update(changes); return PowerFacts(**values)


def finish(c, owner):
    _stage_transition(c,owner,0,'preparing','command_started')
    _stage_transition(c,owner,1,'command_started','release_pending',outcome='on_observed')
    _stage_transition(c,owner,2,'release_pending','complete')


def test_current_source_registry_footprint_and_repeat(power_engine):
    with power_engine.begin() as c:
        assert apply_python_migration(c,'core.schema.migrations.027_power_target_operations') is False
        assert len(S.pg_functions())==1 and len(S.pg_triggers())==2 and len(S.sqlite_triggers())==3
        M.validate(c)


@pytest.mark.parametrize('style',['connection','session','dbapi'])
def test_caller_transaction_stage_rollback_all_sqlite_styles(power_engine,style):
    if style=='connection':
        c=power_engine.connect(); tx=c.begin(); cleanup=lambda: (tx.rollback(),c.close())
    elif style=='session':
        c=Session(power_engine); c.begin(); cleanup=lambda: (c.rollback(),c.close())
    else:
        raw=power_engine.raw_connection(); c=raw.driver_connection; c.execute('BEGIN'); cleanup=lambda: (c.rollback(),raw.close())
    owner=_stage_operation(c,manual()); finish(c,owner)
    assert S.one(c,'SELECT state FROM '+S.TABLE)[0]=='complete'
    cleanup()
    with power_engine.begin() as other: assert S.one(other,'SELECT 1 FROM '+S.TABLE) is None


def test_manual_exact_global_nonce_replay_no_other_operation(power_engine):
    with power_engine.begin() as c:
        owner=_stage_operation(c,manual()); finish(c,owner)
    with power_engine.begin() as c:
        assert _lookup_replay(c,manual())=='on_observed'
        for facts in [manual(purpose='manual_off'),manual(target_key='8'*64),manual(authority_key='9'*64),manual(actor_user_id=3)]:
            with pytest.raises(PowerStorageError): _lookup_replay(c,facts)
        with pytest.raises(PowerStorageError): _stage_operation(c,manual())
        assert S.one(c,'SELECT count(*) FROM '+S.TABLE)[0]==1


@pytest.mark.parametrize('changes',[{'physical_job_id':None},{'request_nonce':'a'*32},{'actor_user_id':2},{'physical_job_id':True}])
def test_no_fake_automatic_or_mixed_authority(power_engine,changes):
    with power_engine.begin() as c:
        with pytest.raises(PowerStorageError): _stage_operation(c,automatic(**changes))


def test_automatic_positive_without_manual_identity(power_engine):
    with power_engine.begin() as c:
        owner=_stage_operation(c,automatic()); finish(c,owner); M.validate(c)
        assert S.one(c,'SELECT request_nonce,actor_user_id FROM '+S.TABLE)==(None,None)


@pytest.mark.parametrize('state', ['command_started','release_pending','complete','cleanup_uncertain'])
def test_transition_rollback_preserves_original(power_engine,state):
    with power_engine.begin() as c: owner=_stage_operation(c,manual())
    c=power_engine.connect(); tx=c.begin()
    if state=='cleanup_uncertain': _stage_transition(c,owner,0,'preparing',state,reason='worker_uncertain')
    else:
        _stage_transition(c,owner,0,'preparing','command_started')
        if state in ('release_pending','complete'): _stage_transition(c,owner,1,'command_started','release_pending',outcome='off_observed')
        if state=='complete': _stage_transition(c,owner,2,'release_pending','complete')
    tx.rollback(); c.close()
    with power_engine.begin() as c: assert S.one(c,'SELECT state,revision FROM '+S.TABLE)==('preparing',0)


@pytest.mark.parametrize('field,value',[('operation_nonce','a'*32),('printer_id',2),('request_nonce','a'*32),('authority_key','a'*64),('purpose','manual_off'),('owner_nonce','a'*32)])
def test_raw_immutable_identity_refuses(power_engine,field,value):
    with power_engine.begin() as c: _stage_operation(c,manual())
    with pytest.raises(Exception):
        with power_engine.begin() as c: S.execute(c,'UPDATE '+S.TABLE+' SET '+field+'=?',(value,))


def test_terminal_delete_same_state_phase_fabrication_and_replace_recursive_off(power_engine):
    with power_engine.begin() as c:
        owner=_stage_operation(c,manual()); finish(c,owner)
    for statement in ['DELETE FROM '+S.TABLE,"UPDATE "+S.TABLE+" SET revision=revision+1", "UPDATE "+S.TABLE+" SET state='preparing',revision=revision+1"]:
        with pytest.raises(Exception):
            with power_engine.begin() as c: S.execute(c,statement)
    with power_engine.begin() as c:
        S.execute(c,'PRAGMA recursive_triggers=OFF')
        for statement in ['INSERT OR REPLACE INTO '+S.TABLE+' SELECT * FROM '+S.TABLE,'UPDATE OR REPLACE '+S.TABLE+" SET request_nonce='"+'a'*32+"'"]:
            with pytest.raises(Exception): S.execute(c,statement)
        assert S.one(c,'SELECT count(*),state FROM '+S.TABLE)==(1,'complete')


@pytest.mark.parametrize('facts',[manual(request_nonce='a'*32),manual(printer_id=2,request_nonce='a'*32),manual(printer_id=2,target_key='a'*64,request_nonce='a'*32)])
def test_all_active_unique_axes_and_replace_cannot_erase(power_engine,facts):
    with power_engine.begin() as c:
        _stage_operation(c,manual())
        with pytest.raises(Exception): _stage_operation(c,facts)
        S.execute(c,'PRAGMA recursive_triggers=OFF')
        with pytest.raises(Exception): S.execute(c,'INSERT OR REPLACE INTO '+S.TABLE+" SELECT '"+'a'*32+"','"+'b'*32+"',owner_nonce,printer_id,target_key,config_key,destination_key,purpose,physical_job_id,scheduled_job_id,observation_key,actor_user_id,auth_kind,authority_key,state,revision,created_at,changed_at,command_started_at,release_pending_at,completed_at,outcome,cleanup_reason FROM "+S.TABLE)
        assert S.one(c,'SELECT count(*) FROM '+S.TABLE)[0]==1


def test_no_transaction_or_unrecorded_helper_admission(power_engine):
    with power_engine.connect() as c:
        with pytest.raises(MigrationError): _stage_operation(c,manual())
    engine=engine_for()
    with engine.begin() as c:
        ensure_ledger(c); M.apply(c)
        with pytest.raises(PowerStorageError): _stage_operation(c,manual())
    engine.dispose()


def test_fresh_empty_adoption_and_used_unrecorded_refusal(monkeypatch):
    monkeypatch.setattr(migrator,'_ALLOWED_PYTHON_MIGRATIONS',migrator._ALLOWED_PYTHON_MIGRATIONS|{'core.schema.migrations.027_power_target_operations'})
    engine=engine_for()
    with engine.begin() as c: M.apply(c)
    with engine.begin() as c:
        before=S.rows(c,"SELECT name,sql FROM sqlite_master WHERE name=? OR name LIKE 'ptop_%' ORDER BY name",(S.TABLE,))
        assert apply_python_migration(c,'core.schema.migrations.027_power_target_operations') is True
        assert before==S.rows(c,"SELECT name,sql FROM sqlite_master WHERE name=? OR name LIKE 'ptop_%' ORDER BY name",(S.TABLE,))
    with engine.begin() as c: _stage_operation(c,manual()); c.exec_driver_sql("DELETE FROM odin_schema_migrations WHERE migration_id='"+S.MIGRATION_ID+"'")
    with pytest.raises(MigrationError):
        with engine.begin() as c: apply_python_migration(c,'core.schema.migrations.027_power_target_operations')
    engine.dispose()


def test_partial_shape_changed_guard_and_invalid_retained_refuse(power_engine):
    with power_engine.begin() as c: _stage_operation(c,manual())
    with power_engine.begin() as c:
        S.execute(c,'DROP TRIGGER ptop_update_guard'); S.execute(c,'PRAGMA ignore_check_constraints=ON')
        S.execute(c,"UPDATE "+S.TABLE+" SET changed_at='Infinity'")
        S.execute(c,'PRAGMA ignore_check_constraints=OFF'); S.execute(c,S.sqlite_triggers()['ptop_update_guard'])
        with pytest.raises(MigrationError): M.validate(c)
    with power_engine.begin() as c:
        S.execute(c,'DROP TRIGGER ptop_update_guard')
        with pytest.raises(MigrationError): M.validate(c)


def test_fresh_and_adoption_history_failure_roll_back(monkeypatch):
    monkeypatch.setattr(migrator,'_ALLOWED_PYTHON_MIGRATIONS',migrator._ALLOWED_PYTHON_MIGRATIONS|{'core.schema.migrations.027_power_target_operations'})
    for adopt in (False,True):
        engine=engine_for()
        if adopt:
            with engine.begin() as c: M.apply(c)
        with pytest.raises(RuntimeError):
            with engine.begin() as c:
                apply_python_migration(c,'core.schema.migrations.027_power_target_operations'); raise RuntimeError('owned outer commit fault')
        with engine.begin() as c:
            assert bool(S.one(c,"SELECT 1 FROM sqlite_master WHERE name=?",(S.TABLE,)))==adopt
            if S.one(c,"SELECT 1 FROM sqlite_master WHERE name='odin_schema_migrations'"):
                assert S.one(c,'SELECT 1 FROM odin_schema_migrations WHERE migration_id=?',(S.MIGRATION_ID,)) is None
        engine.dispose()

@pytest.mark.parametrize('state',['preparing','command_started','release_pending','complete','cleanup_uncertain'])
def test_every_used_unrecorded_state_refuses_without_new_history(power_engine,state):
    with power_engine.begin() as c:
        owner=_stage_operation(c,manual())
        if state=='cleanup_uncertain': _stage_transition(c,owner,0,'preparing',state,reason='worker_uncertain')
        elif state!='preparing':
            _stage_transition(c,owner,0,'preparing','command_started')
            if state in ('release_pending','complete'): _stage_transition(c,owner,1,'command_started','release_pending',outcome='off_observed')
            if state=='complete': _stage_transition(c,owner,2,'release_pending','complete')
        S.execute(c,'DELETE FROM odin_schema_migrations WHERE migration_id=?',(S.MIGRATION_ID,))
    with pytest.raises(MigrationError,match='Used unrecorded'):
        with power_engine.begin() as c: apply_python_migration(c,'core.schema.migrations.027_power_target_operations')
    with power_engine.begin() as c:
        assert S.one(c,'SELECT state FROM '+S.TABLE)[0]==state
        assert S.one(c,'SELECT 1 FROM odin_schema_migrations WHERE migration_id=?',(S.MIGRATION_ID,)) is None


def test_sqlite_insertion_fence_held_through_adoption_history(tmp_path,monkeypatch):
    monkeypatch.setattr(migrator,'_ALLOWED_PYTHON_MIGRATIONS',migrator._ALLOWED_PYTHON_MIGRATIONS|{'core.schema.migrations.027_power_target_operations'})
    engine=engine_for(tmp_path/'owned-power.db')
    with engine.begin() as c: M.apply(c)
    a=engine.connect();tx=a.begin();M.apply(a)
    b=sqlite3.connect(tmp_path/'owned-power.db',timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError,match='locked'): b.execute('BEGIN IMMEDIATE')
        assert apply_python_migration(a,'core.schema.migrations.027_power_target_operations') is True
        tx.rollback()
    finally:
        b.close();a.close();engine.dispose()


def test_sqlite_unknown_time_cannot_pass_retained_validation(power_engine):
    with power_engine.begin() as c: owner=_stage_operation(c,manual());finish(c,owner)
    with power_engine.begin() as c:
        S.execute(c,'DROP TRIGGER ptop_update_guard');S.execute(c,'PRAGMA ignore_check_constraints=ON')
        S.execute(c,"UPDATE "+S.TABLE+" SET changed_at='not-a-date'")
        with pytest.raises(MigrationError,match='retained state'): S.validate_retained(c)
        S.execute(c,'PRAGMA ignore_check_constraints=OFF')


def test_unreachable_uncertain_phase_check_and_retained_validation(power_engine):
    with power_engine.begin() as c:
        owner = _stage_operation(c, manual())
        for name in S.sqlite_triggers():
            S.execute(c, 'DROP TRIGGER '+name)
        bad = "UPDATE "+S.TABLE+" SET state='cleanup_uncertain',cleanup_reason='worker_uncertain',outcome='on_observed'"
        with pytest.raises(Exception):
            S.execute(c, bad)
        S.execute(c, 'PRAGMA ignore_check_constraints=ON')
        S.execute(c, bad)
        with pytest.raises(MigrationError):
            S.validate_retained(c)
        S.execute(c, 'PRAGMA ignore_check_constraints=OFF')
