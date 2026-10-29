"""Forward-only tray identity schema and released 045 restore contracts."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database, preserved045
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.046_tray_identity')
ROOT = Path(__file__).resolve().parents[2]


def test_frozen045_upgrade_preserves_rows_and_repeat_is_noop(tmp_path):
    code = '''
import runpy,sys
n=runpy.run_path(sys.argv[1],run_name='frozen045')
n['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:045-pause-cancel-alerts' in bootstrap_database(engine)['applied']
with engine.begin() as c:
 c.execute(text("INSERT INTO printers(id,name) VALUES(1,'Fixture')"))
 c.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,color) VALUES(1,1,1,'retained')"))
assert bootstrap_database(engine)['applied']==[]
'''
    result = subprocess.run([sys.executable,'-I','-c',code,str(Path(preserved045.__file__).with_name('runner.py')),str(tmp_path/'old.db')],cwd=tmp_path,capture_output=True,text=True,timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///'+str(tmp_path/'old.db'))
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.execute(text('SELECT color,tray_observation FROM filament_slots')).one() == ('retained',None)
        M.validate(c)


def test_inventory_and_fresh_repeat(tmp_path):
    from modules.system.postgres_backup_service import canonical_tables
    engine=create_engine('sqlite:///'+str(tmp_path/'fresh.db'))
    assert M.MIGRATION_ID in bootstrap_database(engine)['applied']
    assert bootstrap_database(engine)['applied'] == []
    assert 'tray_confirmations' in canonical_tables()
    assert {c['name'] for c in inspect(engine).get_columns('tray_confirmations')} == set(M.COLUMNS)


def test_wrong_existing_shape_refused(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'wrong.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE filament_slots(id INTEGER PRIMARY KEY,tray_observation INTEGER)')
        with pytest.raises(MigrationError,match='observation shape'):
            M.apply(c)


def test_slot_delete_cleans_decisions(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'cleanup.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=ON')
        c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(1,'fixture')")
        c.exec_driver_sql("INSERT INTO filament_library(id,brand,name) VALUES(1,'fixture','fixture')")
        c.exec_driver_sql('INSERT INTO spools(id,filament_id) VALUES(1,1)')
        c.exec_driver_sql('INSERT INTO filament_slots(id,printer_id,slot_number) VALUES(1,1,1)')
        c.exec_driver_sql("INSERT INTO tray_confirmations VALUES('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,'same',1,NULL)")
        c.exec_driver_sql('DELETE FROM filament_slots WHERE id=1')
        assert c.exec_driver_sql('SELECT count(*) FROM tray_confirmations').scalar_one()==0


@pytest.mark.parametrize('version',['2.0.29','2.0.30'])
def test_released045_routing(tmp_path,monkeypatch,version):
    from core.schema import preserved029
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    releases=json.loads((ROOT/'backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert preserved045.ledger_ids()==preserved029.ledger_ids() | set(releases[version])
    archive=tmp_path/'owned.dump';archive.write_bytes(b'x'*100)
    monkeypatch.setattr(backup,'_run_pg_tool',lambda *a:'')
    monkeypatch.setattr(backup,'_toc_inventory',lambda *a:None)
    monkeypatch.setattr(stage,'archive_ledger_ids',lambda *a:preserved045.ledger_ids())
    assert stage.identify_contract(archive,None,None,tmp_path) is stage.PRESERVED045


def test_live_upgrade_from_frozen043_applies_046_after_released_migrations(tmp_path):
    """The historical 043->045 contract stays frozen; live upgrade reaches 046."""
    from core.schema import preserved043
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen043')['prepare_sources']()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
bootstrap_database(create_engine('sqlite:///'+sys.argv[2]))
'''
    result = subprocess.run([sys.executable, '-I', '-c', code,
                             str(Path(preserved043.__file__).with_name('runner.py')),
                             str(tmp_path/'old043.db')], cwd=tmp_path, capture_output=True,
                            text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(tmp_path/'old043.db'))
    try:
        assert bootstrap_database(engine)['applied'] == [
            'python:044-spool-tray-uuid', 'python:045-pause-cancel-alerts', M.MIGRATION_ID]
        assert bootstrap_database(engine)['applied'] == []
        with engine.connect() as connection:
            M.validate(connection)
    finally:
        engine.dispose()
