"""Released 2.3.0 sources stay isolated and upgrade additively to the live schema."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved029, preserved060
from core.schema.preserved060.integrity import check_package, Preserved060Error


def test_released_230_exact_ledger_and_routing(tmp_path, monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    bundle, manifest = check_package()
    root = Path(preserved060.__file__).parent
    releases = json.loads((root.parent/'released_post027_ledgers.json').read_text())['releases']
    assert preserved060.ledger_ids() == preserved029.ledger_ids() | set(releases['2.3.0'])
    assert manifest['source_commit'] == '39dd3b8f97fe716058606d92b03fa9023f0fcdd4'
    assert all(hashlib.sha256((bundle/name).read_bytes()).hexdigest() == digest for name, digest in manifest['sources'].items())
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved060.ledger_ids())
    (tmp_path/'old.dump').write_bytes(b'x' * 128)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    from core.schema import bootstrap
    # While 060 is the newest migration the normal validator handles its archives; once a later
    # migration exists (061 onward) the same archive is routed to this bundle for real.
    if bootstrap.POST027_MIGRATIONS[-1][0] == 'python:060-print-files-stored-path-index':
        assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is None
        monkeypatch.setattr(bootstrap, 'POST027_MIGRATIONS', bootstrap.POST027_MIGRATIONS +
                            (('python:099-future-synthetic', 'core.schema.migrations.099_future'),))
    assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is stage.PRESERVED060


def test_isolated_released_boot_then_live_keeps_catalogue_materials_files_and_history(tmp_path):
    code = '''
import importlib,runpy,sys
bundle,manifest=runpy.run_path(sys.argv[1],run_name='frozen060')['prepare_sources']()
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
assert bootstrap_database(engine)['applied']==[]
assert {'do_not_feed','do_not_feed_note'} <= {c['name'] for c in inspect(engine).get_columns('filament_slots')}
with engine.begin() as c:
 assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
 c.execute(text("INSERT INTO printers(id,name,ace_status) VALUES(5,'Retained printer',:status)"),{'status':'{"state":"ready"}'})
 c.execute(text("INSERT INTO filament_library(id,brand,name,catalogue_type,catalogue_code,catalogue_colors) VALUES(5,'Released','Retained material','ASA Aero','owned-code','[]')"))
 c.execute(text("INSERT INTO print_files(id,filename,stored_path) VALUES(5,'Shared','/owned/shared'),(6,'Alias','/owned/shared')"))
 c.execute(text('INSERT INTO spools(id,filament_id) VALUES(5,5)'))
 c.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,do_not_feed,do_not_feed_note) VALUES(5,5,2,1,'Reserved material')"))
 c.execute(text("UPDATE filament_slots SET filament_type='ASA_AERO' WHERE id=5"))
 for name in ('058_filament_catalogue','059_six_materials','060_print_files_stored_path_index'):
  importlib.import_module('core.schema.migrations.'+name).validate(c)
 c.execute(text("INSERT INTO tray_confirmations(state_id,slot_id,decision,spool_id) VALUES(:state,5,'same',5)"),{'state':'7'*32})
 c.execute(text("INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts) VALUES(1,3,'admin',1,1,5,226,:attempt,:snapshot,'hold','investigation','{}')"),{'attempt':'dr1.'+'a'*64,'snapshot':'ds1.'+'b'*64})
'''
    path = tmp_path/'old.db'
    result = subprocess.run([sys.executable, '-I', '-c', code, str(Path(preserved060.__file__).with_name('runner.py')), str(path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(path))
    with engine.connect() as c:
        old = c.execute(text('SELECT * FROM dispatch_recovery_history')).all()
    first = bootstrap_database(engine)
    from core.schema.bootstrap import POST027_MIGRATIONS
    frozen_ids = preserved060.ledger_ids()
    assert first['applied'] == [mid for mid, _ in POST027_MIGRATIONS if mid not in frozen_ids]
    second = bootstrap_database(engine)
    assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
    with engine.connect() as c:
        assert c.execute(text('SELECT * FROM dispatch_recovery_history')).all() == old
        assert c.execute(text('SELECT name FROM printers WHERE id=5')).scalar_one() == 'Retained printer'
        assert c.execute(text('SELECT count(*) FROM dispatch_recovery_global_history')).scalar_one() == 0
        assert c.execute(text('SELECT ace_status FROM printers WHERE id=5')).scalar_one() == '{"state":"ready"}'
        assert c.execute(text('SELECT printer_id,slot_number,do_not_feed,do_not_feed_note FROM filament_slots WHERE id=5')).one() == (5,2,True,'Reserved material')
        assert c.execute(text('SELECT slot_id,decision,spool_id FROM tray_confirmations WHERE state_id=:state'),{'state':'7'*32}).one() == (5,'same',5)
        assert c.execute(text('SELECT catalogue_type,catalogue_code,catalogue_colors FROM filament_library WHERE id=5')).one() == ('ASA Aero','owned-code','[]')
        assert c.execute(text('SELECT filament_type FROM filament_slots WHERE id=5')).scalar_one() == 'ASA_AERO'
        assert c.execute(text('SELECT id,stored_path FROM print_files ORDER BY id')).all() == [(5,'/owned/shared'),(6,'/owned/shared')]
        import importlib
        for name in ('058_filament_catalogue','059_six_materials','060_print_files_stored_path_index'):
            importlib.import_module('core.schema.migrations.'+name).validate(c)
    engine.dispose()


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/bootstrap.py', 'overlay/core/schema/migrations/058_filament_catalogue.py', 'overlay/core/schema/migrations/059_six_materials.py', 'overlay/core/schema/migrations/060_print_files_stored_path_index.py'])
def test_tamper_refused(tmp_path, changed):
    source = Path(preserved060.__file__).parent
    target = tmp_path/'preserved060'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved057').symlink_to(source.parent/'preserved057', target_is_directory=True)
    with (target/changed).open('ab') as f:
        f.write(b'\n# altered\n')
    with pytest.raises(Preserved060Error, match='manifest differs|overlay integrity differs'):
        check_package(target)


def test_child_parent_protocol_matches():
    import ast
    tree = ast.parse(Path(preserved060.__file__).with_name('runner.py').read_text())
    eras = {node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'era' for t in node.targets)
            and isinstance(node.value, ast.Constant)}
    assert eras == {60}
    assert preserved060._ACK.fullmatch(b'preserved060-validated:60\n')


def test_stale_060_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal/('.odin-preserved060-'+'a'*20)
    child.mkdir(mode=0o700)
    confirmed = 'odin_validate_'+'b'*20
    intended = 'odin_validate_'+'c'*20
    (child/confirmed).write_text('')
    (child/(intended+'.intent')).write_text('')
    work = tmp_path/('.odin-preserved060-'+'d'*20)
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


def test_released060_restore_closure_imports_only_frozen_sources():
    from tests.test_contracts.preserved_restore_helpers import run_frozen, RESTORE_CLOSURE
    result = run_frozen(Path(preserved060.__file__).with_name('runner.py'), RESTORE_CLOSURE)
    assert result.returncode == 0, result.stderr
    assert 'closure-imported' in result.stdout


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved060 stage ledger differs: owned fixture', True),
    ('Preserved060 package hash differs', False),
])
def test_parent_side_errors_are_classified(tmp_path, monkeypatch, message, archive_refusal):
    from unittest.mock import MagicMock
    from core import database_config
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: stage.PRESERVED060)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *a: ('unused', 'unused'))
    engine = MagicMock()
    engine.execution_options.return_value = engine
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *a, **k: engine)
    for name in ('sweep_stale', 'record', 'forget', 'drop_database'):
        monkeypatch.setattr(restore_artifacts, name, lambda *a, **k: None)
    def refuse(*a, **k):
        raise preserved060.Preserved060Error(message)
    monkeypatch.setattr(preserved060, 'restore_into_stage', refuse)
    expected = backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected, match=message):
        with stage.prepared_archive(archive, 'postgresql://localhost/target', 'postgresql://localhost/maintenance', None, None, tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')

