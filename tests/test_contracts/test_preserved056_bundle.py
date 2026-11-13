"""Released 2.1.0 sources stay isolated and upgrade additively through 057."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved029, preserved056
from core.schema.preserved056.integrity import check_package, Preserved056Error


def test_released_210_exact_ledger_and_routing(tmp_path, monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    bundle, manifest = check_package()
    root = Path(preserved056.__file__).parent
    releases = json.loads((root.parent/'released_post027_ledgers.json').read_text())['releases']
    assert preserved056.ledger_ids() == preserved029.ledger_ids() | set(releases['2.1.0'])
    assert manifest['source_commit'] == '9112d4071b88d33dbbc5b82da558dfdf74d663db'
    assert all(hashlib.sha256((bundle/name).read_bytes()).hexdigest() == digest for name, digest in manifest['sources'].items())
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved056.ledger_ids())
    (tmp_path/'old.dump').write_bytes(b'x' * 128)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is stage.PRESERVED056


def test_isolated_released_boot_then_057_keeps_org_history(tmp_path):
    code = '''
import runpy,sys
bundle,manifest=runpy.run_path(sys.argv[1],run_name='frozen056')['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
assert bootstrap_database(engine)['applied']==[]
assert 'do_not_feed' not in {c['name'] for c in inspect(engine).get_columns('filament_slots')}
with engine.begin() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
 c.execute(text("INSERT INTO printers(id,name) VALUES(5,'Retained printer')"))
 c.execute(text("INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts) VALUES(1,3,'admin',1,1,5,226,:attempt,:snapshot,'hold','investigation','{}')"),{'attempt':'dr1.'+'a'*64,'snapshot':'ds1.'+'b'*64})
'''
    path = tmp_path/'old.db'
    result = subprocess.run([sys.executable, '-I', '-c', code, str(Path(preserved056.__file__).with_name('runner.py')), str(path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(path))
    with engine.connect() as c:
        old = c.execute(text('SELECT * FROM dispatch_recovery_history')).all()
    first = bootstrap_database(engine)
    assert first['applied'] == ['python:057-slot-feed-policy']
    second = bootstrap_database(engine)
    assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
    with engine.connect() as c:
        assert c.execute(text('SELECT * FROM dispatch_recovery_history')).all() == old
        assert c.execute(text('SELECT name FROM printers WHERE id=5')).scalar_one() == 'Retained printer'
        assert c.execute(text('SELECT count(*) FROM dispatch_recovery_global_history')).scalar_one() == 0
    engine.dispose()


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/bootstrap.py'])
def test_tamper_refused(tmp_path, changed):
    source = Path(preserved056.__file__).parent
    target = tmp_path/'preserved056'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved055').symlink_to(source.parent/'preserved055', target_is_directory=True)
    with (target/changed).open('ab') as f:
        f.write(b'\n# altered\n')
    with pytest.raises(Preserved056Error, match='manifest differs|overlay integrity differs'):
        check_package(target)


def test_child_parent_protocol_matches():
    import ast
    tree = ast.parse(Path(preserved056.__file__).with_name('runner.py').read_text())
    eras = {node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'era' for t in node.targets)
            and isinstance(node.value, ast.Constant)}
    assert eras == {56}
    assert preserved056._ACK.fullmatch(b'preserved056-validated:56\n')


def test_stale_056_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal/('.odin-preserved056-'+'a'*20)
    child.mkdir(mode=0o700)
    confirmed = 'odin_validate_'+'b'*20
    intended = 'odin_validate_'+'c'*20
    (child/confirmed).write_text('')
    (child/(intended+'.intent')).write_text('')
    work = tmp_path/('.odin-preserved056-'+'d'*20)
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


def test_released056_restore_closure_imports_only_frozen_sources():
    from tests.test_contracts.preserved_restore_helpers import run_frozen, RESTORE_CLOSURE
    result = run_frozen(Path(preserved056.__file__).with_name('runner.py'), RESTORE_CLOSURE)
    assert result.returncode == 0, result.stderr
    assert 'closure-imported' in result.stdout
