"""2.0.23's exact 040 schema remains restorable without current 040 sources."""
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
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(runner),str(tmp_path/'frozen.db')],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr


def test_exact_2023_ledger():
    from core.schema import preserved039
    assert preserved040.ledger_ids()==preserved039.ledger_ids()|{'python:040-job-foundation'}
    released=json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']['2.0.23']
    assert set(released)<=preserved040.ledger_ids()
    assert 'python:041-dispatch-capability' not in preserved040.ledger_ids()


@pytest.mark.parametrize('changed',['manifest.json','overlay/core/schema/migrations/040_job_foundation.py'])
def test_tamper_refused(tmp_path,changed):
    from core.schema.preserved040.integrity import check_package,Preserved040Error
    base=Path(preserved040.__file__).parent;target=tmp_path/'preserved040'
    shutil.copytree(base,target,ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved039').symlink_to(base.parent/'preserved039',target_is_directory=True)
    with (target/changed).open('ab') as file:file.write(b'\n# changed\n')
    with pytest.raises(Preserved040Error,match='differs'):check_package(target)
