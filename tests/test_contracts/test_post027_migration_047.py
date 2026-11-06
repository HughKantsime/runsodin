"""Frozen046 upgrade, backfill, shape refusals, and canonical inventories."""
from core.schema.audit_integrity_version import MODULE as AUDIT_MIGRATION_MODULE, MIGRATION_ID as AUDIT_MIGRATION_ID
import importlib
from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved046
from core.schema.migrator import MigrationError
from core.audit_integrity import verify

M = importlib.import_module(AUDIT_MIGRATION_MODULE)


def test_frozen046_upgrade_preserves_history_and_backfills_once(tmp_path):
    path=tmp_path/'old.db'
    code='''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen046')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2]);bootstrap_database(engine)
with engine.begin() as c:
 c.execute(text("INSERT INTO audit_logs(action,details) VALUES('retained','{}')"))
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(Path(preserved046.__file__).with_name('runner.py')),str(path)],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr
    from modules.system.backup_service import validate_database, stage_restore, apply_pending_restore
    assert validate_database(path)['table_count'] > 30
    live = tmp_path/'live.db'
    live_engine = create_engine('sqlite:///'+str(live)); bootstrap_database(live_engine); live_engine.dispose()
    stage_restore(path, 'sqlite:///'+str(live))
    apply_pending_restore('sqlite:///'+str(live))
    path = live
    engine=create_engine('sqlite:///'+str(path))
    assert bootstrap_database(engine)['applied']==[M.MIGRATION_ID, 'python:048-training-signoff', 'python:049-education-classes', 'python:050-education-program-reports']
    with engine.begin() as c:
        assert verify(c)['ok']
        assert c.execute(text('SELECT action FROM audit_logs')).scalar_one()=='retained'
        original=c.execute(text('SELECT row_hash FROM audit_logs')).scalar_one()
    assert bootstrap_database(engine)['applied']==[]
    with engine.connect() as c:
        assert c.execute(text('SELECT row_hash FROM audit_logs')).scalar_one()==original


def test_shape_registry_and_repeat(tmp_path):
    from modules.system.postgres_backup_service import canonical_tables
    from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
    engine=create_engine('sqlite:///'+str(tmp_path/'fresh.db'))
    assert M.MIGRATION_ID in bootstrap_database(engine)['applied']
    assert bootstrap_database(engine)['applied']==[]
    assert set(M.TABLES)<=canonical_tables()
    assert 'odin_audit_append' in canonical_functions()
    assert ('audit_logs','odin_audit_append') in canonical_triggers()
    with engine.begin() as c:
        M.validate(c)
        c.exec_driver_sql('ALTER TABLE audit_siem_outbox ADD COLUMN rogue TEXT')
        with pytest.raises(MigrationError,match='shape'):
            M.validate(c)


def test_trigger_mutation_is_refused(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'trigger.db'));bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql('DROP TRIGGER odin_audit_append')
        with pytest.raises(MigrationError,match='trigger'):
            M.validate(c)


def test_source_pin_refuses_drift_before_schema_changes(tmp_path,monkeypatch):
    import hashlib
    core=Path(M.__file__).resolve().parents[2]
    assert all(hashlib.sha256((core/name).read_bytes()).hexdigest()==digest for name,digest in M.SOURCE_PINS.items())
    engine=create_engine('sqlite:///'+str(tmp_path/'pins.db'))
    monkeypatch.setattr(M,'SOURCE_PINS',{'audit_integrity.py':'0'*64})
    with engine.begin() as c:
        with pytest.raises(MigrationError,match='source contract'):
            M.apply(c)
        assert not __import__('sqlalchemy').inspect(c).has_table('audit_chain_state')


def test_backfill_uses_batched_updates_not_per_row_queries(tmp_path):
    from sqlalchemy import event
    engine = create_engine('sqlite:///'+str(tmp_path/'backfill.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql('DROP TRIGGER odin_audit_append')
        c.execute(text('DELETE FROM audit_chain_state'))
        c.execute(text("INSERT INTO audit_logs(action,details) VALUES('legacy','{}')"), [{}]*600)
    updates = []
    def observe(c, cursor, statement, parameters, context, many):
        if statement.startswith('UPDATE audit_logs SET prev_hash='):
            updates.append((many, len(parameters)))
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        with engine.begin() as c:
            M.apply(c)
            assert verify(c)['ok']
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
        engine.dispose()
    assert updates == [(True, 500), (True, 100)]
