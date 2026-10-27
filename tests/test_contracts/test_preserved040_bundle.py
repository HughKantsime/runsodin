"""2.0.22's exact 040 schema remains restorable without current 040 sources."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from core.schema import preserved040


def test_isolated040_boot_repeat_and_ledger(tmp_path):
    runner=Path(preserved040.__file__).with_name('runner.py')
    code='''
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='frozen040')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:040-job-foundation' in bootstrap_database(engine)['applied']
assert 'terminal_filament_measurements' in inspect(engine).get_table_names()
assert 'job_summary_outbox' in inspect(engine).get_table_names()
assert 'color_name' not in {c['name'] for c in inspect(engine).get_columns('spools')}
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(runner),str(tmp_path/'frozen.db')],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr


def test_exact_2022_ledger():
    from core.schema import preserved039
    assert preserved040.ledger_ids()==preserved039.ledger_ids()|{'python:040-job-foundation'}
    released=json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']['2.0.22']
    assert set(released)<=preserved040.ledger_ids()
    assert 'python:041-inventory-color' not in preserved040.ledger_ids()


@pytest.mark.parametrize('changed',['manifest.json','overlay/core/schema/migrations/040_job_foundation.py'])
def test_tamper_refused(tmp_path,changed):
    from core.schema.preserved040.integrity import check_package,Preserved040Error
    base=Path(preserved040.__file__).parent;target=tmp_path/'preserved040'
    shutil.copytree(base,target,ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved039').symlink_to(base.parent/'preserved039',target_is_directory=True)
    with (target/changed).open('ab') as file:file.write(b'\n# changed\n')
    with pytest.raises(Preserved040Error,match='differs'):check_package(target)


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved040 stage ledger differs: owned fixture', True),
    ('Preserved040 package hash differs', False),
])
def test_restore_classifies_preserved040_stage_errors(tmp_path,monkeypatch,message,archive_refusal):
    from unittest.mock import MagicMock
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    from core import database_config
    archive=tmp_path/'owned.dump';archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage,'identify_contract',lambda *a:stage.PRESERVED040)
    monkeypatch.setattr(backup,'_require_distinct_maintenance_secret',lambda *a:None)
    monkeypatch.setattr(backup,'_maintenance_urls',lambda *a:('unused','unused'))
    engine=MagicMock();engine.execution_options.return_value=engine
    monkeypatch.setattr(database_config,'create_database_engine',lambda *a,**k:engine)
    for name in ('sweep_stale','record','forget','drop_database'):
        monkeypatch.setattr(restore_artifacts,name,lambda *a,**k:None)
    def refuse(*a,**k): raise preserved040.Preserved040Error(message)
    monkeypatch.setattr(preserved040,'restore_into_stage',refuse)
    expected=backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected,match=message):
        with stage.prepared_archive(archive,'postgresql://localhost/target','postgresql://localhost/maintenance',None,None,tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')
