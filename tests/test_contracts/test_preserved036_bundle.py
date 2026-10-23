"""Released 2.0.9 (036) restore sources stay isolated from migration 037."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from core.schema import preserved036


def test_preserved036_boots_isolated_without_current_memberships(tmp_path):
    runner = Path(preserved036.__file__).with_name('runner.py')
    code = """
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='ctec_frozen')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
first=bootstrap_database(engine)
assert 'python:036-ctec-pilot' in first['applied']
assert 'python:037-education-memberships' not in first['applied']
assert 'education_memberships' not in inspect(engine).get_table_names()
assert 'archived_at' in {c['name'] for c in inspect(engine).get_columns('education_submissions')}
assert 'hidden_from_library' in {c['name'] for c in inspect(engine).get_columns('models')}
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
assert len(manifest['ledger_ids'])==57
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(runner), str(tmp_path/'frozen.db')], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_preserved036_exact_released_ledger():
    from core.schema import preserved035
    assert preserved036.ledger_ids() == preserved035.ledger_ids() | {'python:036-ctec-pilot'}
    ledger=json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert set(ledger['2.0.9']) <= preserved036.ledger_ids()
    assert 'python:037-education-memberships' not in preserved036.ledger_ids()


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/migrations/036_ctec_pilot.py'])
def test_preserved036_tampered_sources_refused(tmp_path, changed):
    from core.schema.preserved036.integrity import check_package,Preserved036Error
    base=Path(preserved036.__file__).parent
    target=tmp_path/'preserved036'
    shutil.copytree(base,target,ignore=shutil.ignore_patterns('__pycache__'))
    # Base package resolution is anchored to this sibling; no live-source fallback.
    (tmp_path/'preserved035').symlink_to(base.parent/'preserved035',target_is_directory=True)
    with (target/changed).open('ab') as file:file.write(b'\n# changed\n')
    with pytest.raises(Preserved036Error,match='differs'):
        check_package(target)
