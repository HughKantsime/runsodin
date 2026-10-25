"""2.0.17's exact 039 schema remains restorable without current 039 sources."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from core.schema import preserved039


def test_isolated039_boot_repeat_and_ledger(tmp_path):
    runner=Path(preserved039.__file__).with_name('runner.py')
    code='''
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='frozen039')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:039-measured-filament' in bootstrap_database(engine)['applied']
assert 'terminal_filament_measurements' in inspect(engine).get_table_names()
assert 'job_summary_outbox' not in inspect(engine).get_table_names()
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
'''
    result=subprocess.run([sys.executable,'-I','-c',code,str(runner),str(tmp_path/'frozen.db')],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stderr


def test_exact_2017_ledger():
    from core.schema import preserved038
    assert preserved039.ledger_ids()==preserved038.ledger_ids()|{'python:039-measured-filament'}
    released=json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']['2.0.17']
    assert set(released)<=preserved039.ledger_ids()
    assert 'python:040-job-foundation' not in preserved039.ledger_ids()


@pytest.mark.parametrize('changed',['manifest.json','overlay/core/schema/migrations/039_measured_filament.py'])
def test_tamper_refused(tmp_path,changed):
    from core.schema.preserved039.integrity import check_package,Preserved039Error
    base=Path(preserved039.__file__).parent;target=tmp_path/'preserved039'
    shutil.copytree(base,target,ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved038').symlink_to(base.parent/'preserved038',target_is_directory=True)
    with (target/changed).open('ab') as file:file.write(b'\n# changed\n')
    with pytest.raises(Preserved039Error,match='differs'):check_package(target)
