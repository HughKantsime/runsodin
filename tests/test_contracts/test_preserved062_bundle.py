"""Released 2.4.0 sources stay isolated and upgrade additively to the live schema."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database, preserved029, preserved060, preserved062
from core.schema.preserved062.integrity import check_package, Preserved062Error


def test_released_240_exact_ledger_and_routing(tmp_path, monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    bundle, manifest = check_package()
    root = Path(preserved062.__file__).parent
    releases = json.loads((root.parent/'released_post027_ledgers.json').read_text())['releases']
    assert preserved062.ledger_ids() == preserved029.ledger_ids() | set(releases['2.4.0'])
    assert set(manifest['ledger_ids']) - preserved060.ledger_ids() == {
        'python:061-print-filament-evidence', 'python:062-ams-humidity-percent',
    }
    assert manifest['source_commit'] == '5e1f6e4103417a471e1eadfc3099e23ecc7db46f'
    assert all(hashlib.sha256((bundle/name).read_bytes()).hexdigest() == digest for name, digest in manifest['sources'].items())
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved062.ledger_ids())
    (tmp_path/'old.dump').write_bytes(b'x' * 128)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    from core.schema import bootstrap
    # While 062 is the newest migration the normal validator handles its archives; once a later
    # migration exists (063 onward) the same archive is routed to this bundle for real.
    if bootstrap.POST027_MIGRATIONS[-1][0] == 'python:062-ams-humidity-percent':
        assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is None
        monkeypatch.setattr(bootstrap, 'POST027_MIGRATIONS', bootstrap.POST027_MIGRATIONS +
                            (('python:099-future-synthetic', 'core.schema.migrations.099_future'),))
    assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is stage.PRESERVED062


def test_isolated_released_boot_then_live_keeps_filament_evidence_and_humidity(tmp_path):
    from tests.test_contracts.preserved_restore_helpers import run_frozen
    from core.schema.bootstrap import POST027_MIGRATIONS

    path = tmp_path/'released.db'
    result = run_frozen(Path(preserved062.__file__).with_name('runner.py'), '''
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine = create_engine('sqlite:///' + sys.argv[2])
bootstrap_database(engine)
assert bootstrap_database(engine)['applied'] == []
with engine.begin() as c:
    assert set(c.execute(text('SELECT migration_id FROM odin_schema_migrations')).scalars()) == set(manifest['ledger_ids'])
    c.execute(text("INSERT INTO print_filament_evidence(print_job_id,print_birth_nonce,kind,claim,source,spool_id,grams) VALUES(481,:nonce,'usage',1,'backfill',51,12.75)"), {'nonce':'a'*32})
    c.execute(text("INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,humidity_percent) VALUES(5,0,2,37.75),(5,1,3,NULL)"))
engine.dispose()
''', path)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(path))
    try:
        frozen = preserved062.ledger_ids()
        first = bootstrap_database(engine)
        assert first['applied'] == [mid for mid, _ in POST027_MIGRATIONS if mid not in frozen]
        second = bootstrap_database(engine)
        assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
        with engine.connect() as c:
            assert c.execute(text('SELECT print_birth_nonce,kind,claim,source,spool_id,grams FROM print_filament_evidence WHERE print_job_id=481')).one() == ('a'*32,'usage',1,'backfill',51,12.75)
            assert c.execute(text('SELECT ams_unit,humidity,humidity_percent FROM ams_telemetry WHERE printer_id=5 ORDER BY ams_unit')).all() == [(0,2,37.75),(1,3,None)]
    finally:
        engine.dispose()


@pytest.mark.parametrize('changed', [
    'manifest.json', 'overlay/core/schema/bootstrap.py',
    'overlay/core/schema/migrations/061_print_filament_evidence.py',
    'overlay/core/schema/migrations/062_ams_humidity_percent.py',
])
def test_tamper_refused(tmp_path, changed):
    source = Path(preserved062.__file__).parent
    target = tmp_path/'preserved062'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved060').symlink_to(source.parent/'preserved060', target_is_directory=True)
    with (target/changed).open('ab') as f:
        f.write(b'\n# altered\n')
    with pytest.raises(Preserved062Error, match='manifest differs|overlay integrity differs'):
        check_package(target)


def test_child_parent_protocol_matches():
    import ast
    tree = ast.parse(Path(preserved062.__file__).with_name('runner.py').read_text())
    eras = {node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'era' for t in node.targets)
            and isinstance(node.value, ast.Constant)}
    assert eras == {62}
    assert preserved062._ACK.fullmatch(b'preserved062-validated:62\n')


def test_stale_062_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal/('.odin-preserved062-'+'a'*20)
    child.mkdir(mode=0o700)
    confirmed = 'odin_validate_'+'b'*20
    intended = 'odin_validate_'+'c'*20
    (child/confirmed).write_text('')
    (child/(intended+'.intent')).write_text('')
    work = tmp_path/('.odin-preserved062-'+'d'*20)
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


def test_released062_restore_closure_imports_only_frozen_sources():
    from tests.test_contracts.preserved_restore_helpers import run_frozen, RESTORE_CLOSURE
    result = run_frozen(Path(preserved062.__file__).with_name('runner.py'), RESTORE_CLOSURE)
    assert result.returncode == 0, result.stderr
    assert 'closure-imported' in result.stdout


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved062 stage ledger differs: owned fixture', True),
    ('Preserved062 package hash differs', False),
])
def test_parent_side_errors_are_classified(tmp_path, monkeypatch, message, archive_refusal):
    from unittest.mock import MagicMock
    from core import database_config
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: stage.PRESERVED062)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *a: ('unused', 'unused'))
    engine = MagicMock()
    engine.execution_options.return_value = engine
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *a, **k: engine)
    for name in ('sweep_stale', 'record', 'forget', 'drop_database'):
        monkeypatch.setattr(restore_artifacts, name, lambda *a, **k: None)
    def refuse(*a, **k):
        raise preserved062.Preserved062Error(message)
    monkeypatch.setattr(preserved062, 'restore_into_stage', refuse)
    expected = backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected, match=message):
        with stage.prepared_archive(archive, 'postgresql://localhost/target', 'postgresql://localhost/maintenance', None, None, tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')
