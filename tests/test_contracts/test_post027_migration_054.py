"""Additive controlled-part upgrade preserves released data and backup inventory."""
import subprocess
import sys
from pathlib import Path
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved053


def test_released053_upgrade_then_repeat(tmp_path):
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen053')['prepare_sources']()
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
with engine.begin() as c:
 c.execute(text("INSERT INTO printers(id,name) VALUES(1,'Preserved')"))
'''
    result = subprocess.run([sys.executable, '-I', '-c', code,
                             str(Path(preserved053.__file__).with_name('runner.py')), str(tmp_path/'old.db')],
                            cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(tmp_path/'old.db'))
    from core.schema.bootstrap import POST027_MIGRATIONS
    assert bootstrap_database(engine)['applied'] == [mid for mid, _ in POST027_MIGRATIONS if mid not in preserved053.ledger_ids()]
    before = bootstrap_database(engine)
    assert before['applied'] == []
    assert bootstrap_database(engine)['schema_fingerprint'] == before['schema_fingerprint']
    with engine.connect() as c:
        assert c.execute(text('SELECT name FROM printers WHERE id=1')).scalar() == 'Preserved'
        assert c.execute(text('SELECT count(*) FROM controlled_files')).scalar() == 0
    engine.dispose()


def test_released_2045_bundle_is_exact_and_tamper_evident(tmp_path):
    import json
    import shutil
    import pytest
    from core.schema import preserved029
    from core.schema.preserved053.integrity import check_package, Preserved053Error
    source = Path(preserved053.__file__).parent
    _, manifest = check_package()
    releases = json.loads((source.parent / 'released_post027_ledgers.json').read_text())['releases']
    assert preserved053.ledger_ids() == preserved029.ledger_ids() | set(releases['2.0.45'])
    assert manifest['source_commit'] == '89413d548b71db7df2fc00eb5870ff7417dbb2eb'
    target = tmp_path / 'preserved053'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path / 'preserved046').symlink_to(source.parent / 'preserved046', target_is_directory=True)
    changed = target / 'overlay/core/schema/bootstrap.py'
    changed.write_text(changed.read_text() + '\n# altered historical schema\n')
    with pytest.raises(Preserved053Error, match='overlay integrity differs'):
        check_package(target)
