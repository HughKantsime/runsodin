"""Released 2.7.0 sources stay isolated and upgrade additively to the live schema."""
import hashlib
import importlib
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import create_engine
from core.schema import bootstrap_database, preserved029, preserved062, preserved065
from core.schema.preserved065.integrity import check_package, Preserved065Error


def test_released_270_exact_ledger_and_routing(tmp_path, monkeypatch):
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    bundle, manifest = check_package()
    root = Path(preserved065.__file__).parent
    releases = json.loads((root.parent/'released_post027_ledgers.json').read_text())['releases']
    assert releases['2.5.0'] == releases['2.6.0'] == releases['2.7.0']
    assert preserved065.ledger_ids() == preserved029.ledger_ids() | set(releases['2.7.0'])
    assert set(manifest['ledger_ids']) - preserved062.ledger_ids() == {
        'python:063-mfa-recovery-codes', 'python:064-mfa-code-attempts', 'python:065-mfa-last-totp-step',
    }
    assert manifest['source_commit'] == '3a1489b5975e0c8ea07293eddd82215790230f8a'
    assert all(hashlib.sha256((bundle/name).read_bytes()).hexdigest() == digest for name, digest in manifest['sources'].items())
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: preserved065.ledger_ids())
    (tmp_path/'old.dump').write_bytes(b'x' * 128)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    from core.schema import bootstrap
    # While 065 is the newest migration the normal validator handles its archives; once a later
    # migration exists (066 onward) the same archive is routed to this bundle for real.
    if bootstrap.POST027_MIGRATIONS[-1][0] == 'python:065-mfa-last-totp-step':
        assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is None
        monkeypatch.setattr(bootstrap, 'POST027_MIGRATIONS', bootstrap.POST027_MIGRATIONS +
                            (('python:099-future-synthetic', 'core.schema.migrations.099_future'),))
    assert stage.identify_contract(tmp_path/'old.dump', 'postgresql://localhost/odin', None, tmp_path) is stage.PRESERVED065


_SQLITE_ARCHIVE_SETUP = '''
os.environ['DATABASE_URL'] = 'sqlite:///' + sys.argv[2]
sys.argv[1] = str(bundle)
'''


def test_isolated_released_boot_then_live_keeps_mfa_rows(tmp_path):
    from tests.test_contracts.preserved_restore_helpers import run_frozen
    from core.schema.bootstrap import POST027_MIGRATIONS
    from ops.database_parity.post027_restore_native import RELEASED065_ARCHIVE_SOURCE, verify_released065_data

    path = tmp_path/'released.db'
    result = run_frozen(Path(preserved065.__file__).with_name('runner.py'),
                        _SQLITE_ARCHIVE_SETUP + RELEASED065_ARCHIVE_SOURCE, path)
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout.splitlines()[-1])) == preserved065.ledger_ids()
    engine = create_engine('sqlite:///' + str(path))
    try:
        frozen = preserved065.ledger_ids()
        first = bootstrap_database(engine)
        assert first['applied'] == [mid for mid, _ in POST027_MIGRATIONS if mid not in frozen]
        second = bootstrap_database(engine)
        assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
        with engine.connect() as c:
            verify_released065_data(c)
            for name in ('063_mfa_recovery_codes', '064_mfa_code_attempts', '065_mfa_last_totp_step'):
                importlib.import_module('core.schema.migrations.' + name).validate(c)
    finally:
        engine.dispose()


@pytest.mark.parametrize('damage,expected_error', [
    ('DROP INDEX ix_mfa_recovery_codes_user', 'MFA recovery code index differs'),
    ('ALTER TABLE mfa_code_attempts RENAME COLUMN failure_times TO lost', 'MFA code attempt columns differ'),
    ('ALTER TABLE users DROP COLUMN mfa_last_totp_step', 'MFA time step column shape differs'),
])
def test_released_archive_builder_rejects_schema_drift_before_emitting_ledger(tmp_path, damage, expected_error):
    from ops.database_parity.post027_restore_native import RELEASED065_ARCHIVE_SOURCE
    from tests.test_contracts.preserved_restore_helpers import run_frozen

    # Exercise the builder on SQLite with the release's validators. Damage the
    # schema after the last fixture insert, past bootstrap's own validation.
    setup = _SQLITE_ARCHIVE_SETUP + '''
from sqlalchemy import event, Engine
@event.listens_for(Engine, 'after_cursor_execute')
def damage_fixture(conn, cursor, statement, parameters, context, executemany):
    if statement.startswith('INSERT INTO mfa_code_attempts('):
        cursor.execute(sys.argv[3])
'''
    result = run_frozen(Path(preserved065.__file__).with_name('runner.py'),
                        setup + RELEASED065_ARCHIVE_SOURCE, tmp_path/'archive.db', damage)
    assert result.returncode != 0
    assert expected_error in result.stderr
    assert 'python:065-mfa-last-totp-step' not in result.stdout


@pytest.mark.parametrize('changed', [
    'manifest.json', 'overlay/core/schema/bootstrap.py',
    'overlay/core/schema/migrations/063_mfa_recovery_codes.py',
    'overlay/core/schema/migrations/064_mfa_code_attempts.py',
    'overlay/core/schema/migrations/065_mfa_last_totp_step.py',
])
def test_tamper_refused(tmp_path, changed):
    source = Path(preserved065.__file__).parent
    target = tmp_path/'preserved065'
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'preserved062').symlink_to(source.parent/'preserved062', target_is_directory=True)
    with (target/changed).open('ab') as f:
        f.write(b'\n# altered\n')
    with pytest.raises(Preserved065Error, match='manifest differs|overlay integrity differs'):
        check_package(target)


def test_child_parent_protocol_matches():
    import ast
    tree = ast.parse(Path(preserved065.__file__).with_name('runner.py').read_text())
    eras = {node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'era' for t in node.targets)
            and isinstance(node.value, ast.Constant)}
    assert eras == {65}
    assert preserved065._ACK.fullmatch(b'preserved065-validated:65\n')


def test_stale_065_work_and_confirmed_child_journal_are_cleaned(tmp_path):
    import os
    import time
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal/('.odin-preserved065-'+'a'*20)
    child.mkdir(mode=0o700)
    confirmed = 'odin_validate_'+'b'*20
    intended = 'odin_validate_'+'c'*20
    (child/confirmed).write_text('')
    (child/(intended+'.intent')).write_text('')
    work = tmp_path/('.odin-preserved065-'+'d'*20)
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


def test_released065_restore_closure_imports_only_frozen_sources():
    from tests.test_contracts.preserved_restore_helpers import run_frozen, RESTORE_CLOSURE
    result = run_frozen(Path(preserved065.__file__).with_name('runner.py'), RESTORE_CLOSURE)
    assert result.returncode == 0, result.stderr
    assert 'closure-imported' in result.stdout


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved065 stage ledger differs: owned fixture', True),
    ('Preserved065 package hash differs', False),
])
def test_parent_side_errors_are_classified(tmp_path, monkeypatch, message, archive_refusal):
    from unittest.mock import MagicMock
    from core import database_config
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: stage.PRESERVED065)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *a: ('unused', 'unused'))
    engine = MagicMock()
    engine.execution_options.return_value = engine
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *a, **k: engine)
    for name in ('sweep_stale', 'record', 'forget', 'drop_database'):
        monkeypatch.setattr(restore_artifacts, name, lambda *a, **k: None)
    def refuse(*a, **k):
        raise preserved065.Preserved065Error(message)
    monkeypatch.setattr(preserved065, 'restore_into_stage', refuse)
    expected = backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected, match=message):
        with stage.prepared_archive(archive, 'postgresql://localhost/target', 'postgresql://localhost/maintenance', None, None, tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')
