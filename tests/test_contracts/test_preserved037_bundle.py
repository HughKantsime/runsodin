"""Released 2.0.12 (037) restore sources stay isolated from migration 038."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from core.schema import preserved037


def test_preserved037_boots_isolated_without_current_recovery(tmp_path):
    runner = Path(preserved037.__file__).with_name('runner.py')
    code = """
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='ctec_frozen')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
first=bootstrap_database(engine)
assert 'python:037-education-memberships' in first['applied']
assert 'python:038-dispatch-recovery' not in first['applied']
assert 'education_memberships' in inspect(engine).get_table_names()
assert 'dispatch_recovery_history' not in inspect(engine).get_table_names()
assert 'archived_at' in {c['name'] for c in inspect(engine).get_columns('education_submissions')}
assert 'hidden_from_library' in {c['name'] for c in inspect(engine).get_columns('models')}
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
assert len(manifest['ledger_ids'])==58
from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
functions,triggers=canonical_functions(),canonical_triggers()
assert (len(functions),len(triggers))==(28,38)
assert 'odin_recovery_immutable' not in functions
assert not any(table=='dispatch_recovery_history' for table,name in triggers)
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(runner), str(tmp_path/'frozen.db')], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_preserved037_exact_released_ledger():
    from core.schema import preserved036
    assert preserved037.ledger_ids() == preserved036.ledger_ids() | {'python:037-education-memberships'}
    ledger=json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert set(ledger['2.0.12']) <= preserved037.ledger_ids()
    assert 'python:038-dispatch-recovery' not in preserved037.ledger_ids()


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/migrations/037_education_memberships.py'])
def test_preserved037_tampered_sources_refused(tmp_path, changed):
    from core.schema.preserved037.integrity import check_package,Preserved037Error
    base=Path(preserved037.__file__).parent
    target=tmp_path/'preserved037'
    shutil.copytree(base,target,ignore=shutil.ignore_patterns('__pycache__'))
    # Base package resolution is anchored to this sibling; no live-source fallback.
    (tmp_path/'preserved036').symlink_to(base.parent/'preserved036',target_is_directory=True)
    with (target/changed).open('ab') as file:file.write(b'\n# changed\n')
    with pytest.raises(Preserved037Error,match='differs'):
        check_package(target)
