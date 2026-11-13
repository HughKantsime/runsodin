"""Migration 057: frozen 056 upgrade, fresh/repeat, shape and restore routing."""
import importlib
import subprocess
import sys
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text, inspect
from core.schema import bootstrap_database, preserved056
from core.schema.migrator import MigrationError
M=importlib.import_module('core.schema.migrations.057_slot_feed_policy')


def test_frozen056_upgrade_preserves_rows(tmp_path):
    code='''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen056')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:056-dispatch-recovery-global' in bootstrap_database(engine)['applied']
with engine.begin() as c:
 c.execute(text("INSERT INTO printers(id,name) VALUES(1,'Fixture')"))
 c.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,color) VALUES(1,1,1,'retained')"))
assert bootstrap_database(engine)['applied']==[]
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(Path(preserved056.__file__).with_name('runner.py')),str(tmp_path/'old.db')],cwd=tmp_path,capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr
    engine=create_engine('sqlite:///'+str(tmp_path/'old.db'))
    assert bootstrap_database(engine)['applied']==[M.MIGRATION_ID]
    assert bootstrap_database(engine)['applied']==[]
    with engine.connect() as c:
        assert c.execute(text('SELECT color,do_not_feed,do_not_feed_note FROM filament_slots')).one()==('retained',0,None)
        M.validate(c)


def test_fresh_boot_repeat_and_inventory(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'fresh.db'))
    assert M.MIGRATION_ID in bootstrap_database(engine)['applied']
    assert bootstrap_database(engine)['applied']==[]
    assert {'do_not_feed','do_not_feed_note'} <= {c['name'] for c in inspect(engine).get_columns('filament_slots')}
    with engine.connect() as c:M.validate(c)


def test_wrong_shape_refused(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'bad.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE filament_slots(id INTEGER PRIMARY KEY,do_not_feed INTEGER)')
        c.exec_driver_sql('CREATE TABLE printers(id INTEGER PRIMARY KEY)')
        with pytest.raises(MigrationError,match='shape differs'):M.apply(c)


def test_released056_restores_routed(tmp_path,monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    monkeypatch.setattr(backup,'_run_pg_tool',lambda *a:'')
    monkeypatch.setattr(backup,'_toc_inventory',lambda *a:None)
    monkeypatch.setattr(stage,'archive_ledger_ids',lambda *a:preserved056.ledger_ids())
    archive=tmp_path/'owned.dump';archive.write_bytes(b'x'*100)
    assert stage.identify_contract(archive,None,None,tmp_path) is stage.PRESERVED056
