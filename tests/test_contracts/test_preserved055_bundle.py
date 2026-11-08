"""Released 2.0.49 sources stay isolated and upgrade additively through 056."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved029, preserved055
from core.schema.preserved055.integrity import check_package, Preserved055Error


def test_released_2049_exact_ledger_and_routing(tmp_path, monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    bundle, manifest = check_package()
    root = Path(preserved055.__file__).parent
    releases = json.loads((root.parent/'released_post027_ledgers.json').read_text())['releases']
    assert preserved055.ledger_ids() == preserved029.ledger_ids() | set(releases['2.0.49'])
    assert manifest['source_commit'] == '90d901ba86df7fee4ecaf99f69d08f48cd835c21'
    assert all(hashlib.sha256((bundle/name).read_bytes()).hexdigest() == digest for name, digest in manifest['sources'].items())
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved055.ledger_ids())
    (tmp_path/'old.dump').write_bytes(b'x' * 128)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is stage.PRESERVED055


def test_isolated_released_boot_then_056_keeps_org_history(tmp_path):
    code = '''
import runpy,sys
bundle,manifest=runpy.run_path(sys.argv[1],run_name='frozen055')['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
assert bootstrap_database(engine)['applied']==[]
assert 'dispatch_recovery_global_history' not in inspect(engine).get_table_names()
with engine.begin() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
 c.execute(text("INSERT INTO printers(id,name) VALUES(5,'Retained printer')"))
 c.execute(text("INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts) VALUES(1,3,'admin',1,1,5,226,:attempt,:snapshot,'hold','investigation','{}')"),{'attempt':'dr1.'+'a'*64,'snapshot':'ds1.'+'b'*64})
'''
    path = tmp_path/'old.db'
    result = subprocess.run([sys.executable, '-I', '-c', code, str(Path(preserved055.__file__).with_name('runner.py')), str(path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(path))
    with engine.connect() as c:
        old = c.execute(text('SELECT * FROM dispatch_recovery_history')).all()
    first = bootstrap_database(engine)
    assert first['applied'] == ['python:056-dispatch-recovery-global']
    second = bootstrap_database(engine)
    assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
    with engine.connect() as c:
        assert c.execute(text('SELECT * FROM dispatch_recovery_history')).all() == old
        assert c.execute(text('SELECT name FROM printers WHERE id=5')).scalar_one() == 'Retained printer'
        assert c.execute(text('SELECT count(*) FROM dispatch_recovery_global_history')).scalar_one() == 0
    engine.dispose()


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/bootstrap.py'])
def test_tamper_refused(tmp_path, changed):
    source = Path(preserved055.__file__).parent
    target = tmp_path/'preserved055'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved053').symlink_to(source.parent/'preserved053', target_is_directory=True)
    with (target/changed).open('ab') as f:
        f.write(b'\n# altered\n')
    with pytest.raises(Preserved055Error, match='manifest differs|overlay integrity differs'):
        check_package(target)


def test_child_parent_protocol_matches():
    import ast
    tree = ast.parse(Path(preserved055.__file__).with_name('runner.py').read_text())
    eras = {node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'era' for t in node.targets)
            and isinstance(node.value, ast.Constant)}
    assert eras == {55}
    assert preserved055._ACK.fullmatch(b'preserved055-validated:55\n')


def test_stale_055_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal/('.odin-preserved055-'+'a'*20)
    child.mkdir(mode=0o700)
    confirmed = 'odin_validate_'+'b'*20
    intended = 'odin_validate_'+'c'*20
    (child/confirmed).write_text('')
    (child/(intended+'.intent')).write_text('')
    work = tmp_path/('.odin-preserved055-'+'d'*20)
    work.mkdir()
    unrelated = tmp_path/'keep-me'
    unrelated.mkdir()
    old = time.time()-7*3600
    for path in (child/confirmed, child/(intended+'.intent'), child, work, unrelated):
        os.utime(path, (old, old))
    asked = []
    result = artifacts.sweep_stale(tmp_path, 'unused', None, drop=lambda names: asked.extend(names) or names)
    assert asked == [confirmed] and result['databases'] == [confirmed]
    assert not child.exists() and not work.exists() and unrelated.exists()
