"""preserved046: the complete released 2.0.36 sources, runnable and honest about their era.

The decisive proof is the native PostgreSQL archive round trip in
ops/database_parity/post027_restore_native.py. These contracts keep the package from
drifting away from what that proof exercised.
"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pytest
from sqlalchemy import create_engine, inspect, text

from core.schema import bootstrap_database, preserved045, preserved046
from ops.database_parity import freeze_preserved_bundle as freeze

RELEASE_COMMIT = '0b7a55318a640d4aac57ff82f5d15990c65641c6'
RELEASE = '2.0.36'
MIGRATION_046 = 'python:046-tray-identity'
MIGRATION_047 = 'python:047-slot-feed-policy'
PACKAGE = Path(preserved046.__file__).parent
RUNNER = PACKAGE / 'runner.py'
MANIFEST = json.loads((PACKAGE / 'manifest.json').read_text())
BASE_MANIFEST = json.loads((Path(preserved045.__file__).parent / 'manifest.json').read_text())


def _released_commit_available():
    if shutil.which('git') is None:
        return False
    return subprocess.run(['git', 'cat-file', '-e', RELEASE_COMMIT + '^{commit}'], cwd=freeze.ROOT,
                          capture_output=True).returncode == 0


# The same closed environment runner.main() gives the frozen sources.
RESTORE_ENV_PROGRAM = '''
import os,runpy,sys,tempfile
folder=tempfile.mkdtemp(prefix='frozen-child-')
os.environ.update(DATABASE_URL='postgresql://owned@127.0.0.1:1/odin_validate_'+'a'*20,DATABASE_PASSWORD_FILE='/dev/null',
                  ODIN_DATA_DIR=folder,ODIN_DB_ROLE='restore',JWT_SECRET_KEY='k'*64,LANG='C.UTF-8',PYTHONWARNINGS='ignore')
namespace=runpy.run_path(sys.argv[1],run_name='frozen')
bundle,manifest=namespace['prepare_sources']()
'''


def run_frozen(runner, body, *args, timeout=120):
    """Run ``body`` in the bundle's isolated interpreter, with its closed-source importer active."""
    return subprocess.run([sys.executable, '-I', '-c', RESTORE_ENV_PROGRAM + body, str(runner), *map(str, args)],
                          capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir())


# What runner.main() itself imports before it touches any database.
RESTORE_CLOSURE = '''
from core.database_config import create_database_engine
from core.schema import bootstrap_database,validate_schema
from modules.system import postgres_backup_service as b
from modules.system.postgres_canonical_executables import validate_executables
assert b.__file__.startswith(str(bundle)),b.__file__
assert callable(b._validate_current_postgres_restore) and callable(b._restore_postgres_archive) and callable(validate_executables)
print('closure-imported',len(b.canonical_tables()))
'''


def test_manifest_records_the_released_commit_and_its_era():
    assert MANIFEST['profile'] == 'restore-046'
    assert MANIFEST['source_commit'] == RELEASE_COMMIT
    for fact in (RELEASE, 'schema through 046', 'preserved045'):
        assert fact in MANIFEST['source_note']
    assert set(MANIFEST['ledger_ids']) == set(BASE_MANIFEST['ledger_ids']) | {MIGRATION_046}
    assert MANIFEST['ledger_ids'] == sorted(MANIFEST['ledger_ids'])
    assert MIGRATION_047 not in MANIFEST['ledger_ids']
    assert not [name for name in MANIFEST['sources'] if '/047_' in name]
    assert MANIFEST['sql_paths'] == sorted(name for name in MANIFEST['sources'] if name.endswith('.sql'))
    assert not [name for name in MANIFEST['sources'] if name.startswith('core/schema/preserved')]


@pytest.mark.skipif(not _released_commit_available(), reason='Released commit is not in this checkout')
def test_bundle_is_exactly_what_the_released_commit_generates():
    """Every source byte, the overlay and all four control files, recomputed from Git objects."""
    manifest, overlay = freeze.build(RELEASE_COMMIT, RELEASE, 46, 45, migration=MIGRATION_046)
    control = freeze.control_files(manifest, 46, 45, migration=MIGRATION_046)
    assert freeze.check(PACKAGE, manifest, overlay, control) == []
    assert manifest == MANIFEST


@pytest.mark.skipif(not _released_commit_available(), reason='Released commit is not in this checkout')
def test_released_commit_is_the_2_0_36_release_and_stops_at_046():
    ids = freeze.verify_release(RELEASE_COMMIT, RELEASE, 46)
    assert ids[-1] == MIGRATION_046
    releases = json.loads(freeze.blob(RELEASE_COMMIT, 'backend/core/schema/released_post027_ledgers.json'))['releases']
    assert releases['2.0.35'] == releases['2.0.36'] == ids


@pytest.mark.skipif(not _released_commit_available(), reason='Released commit is not in this checkout')
@pytest.mark.parametrize('commit,release,era,migration,refusal', [
    (RELEASE_COMMIT[:8], RELEASE, 46, MIGRATION_046, 'full 40-character'),
    (RELEASE_COMMIT, '2.0.35', 46, MIGRATION_046, 'VERSION is 2.0.36'),
    (RELEASE_COMMIT, RELEASE, 47, MIGRATION_046, 'does not end at era 47'),
    (RELEASE_COMMIT, RELEASE, 46, MIGRATION_047, 'not python:047'),
])
def test_generator_refuses_the_wrong_release_inputs(commit, release, era, migration, refusal):
    with pytest.raises(freeze.FreezeError, match=refusal):
        freeze.build(commit, release, era, 45, migration=migration)


def test_overlay_is_exactly_the_hash_difference_from_the_base():
    expected = {name for name, digest in MANIFEST['sources'].items() if BASE_MANIFEST['sources'].get(name) != digest}
    actual = {path.relative_to(PACKAGE / 'overlay').as_posix() for path in (PACKAGE / 'overlay').rglob('*') if path.is_file()}
    assert actual == expected


def test_materialized_bundle_is_closed_over_the_manifest():
    bundle, manifest = preserved046.check_package()
    materialized = {path.relative_to(bundle).as_posix() for path in bundle.rglob('*')
                    if path.is_file() and '__pycache__' not in path.parts}
    assert materialized == set(manifest['sources'])


def test_isolated_child_imports_the_real_postgres_restore_closure():
    """The historical bundles' failure: bootstrap imports, PostgreSQL restore does not."""
    result = run_frozen(RUNNER, RESTORE_CLOSURE + '''
import modules.system.backup_failure_alerts as alerts
assert alerts.__file__.startswith(str(bundle)),alerts.__file__
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith('closure-imported')


FROZEN_BOOT = '''
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:046-tray-identity' in bootstrap_database(engine)['applied']
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
    assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
assert 'tray_confirmations' in inspect(engine).get_table_names()
slot={c['name'] for c in inspect(engine).get_columns('filament_slots')}
assert 'tray_observation' in slot and not slot&{'do_not_feed','do_not_feed_note'}
assert 'ace_status' not in {c['name'] for c in inspect(engine).get_columns('printers')}
with engine.begin() as c:
    c.execute(text("INSERT INTO printers(id,name) VALUES(1,'Fixture')"))
    c.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(1,'Fixture','PLA')"))
    c.execute(text('INSERT INTO spools(id,filament_id) VALUES(1,1)'))
    c.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,tray_observation) VALUES(1,1,1,:state)"),{'state':'{"id":"kept"}'})
    c.execute(text("INSERT INTO tray_confirmations(state_id,slot_id,decision,spool_id) VALUES(:state,1,'same',1)"),{'state':'6'*32})
engine.dispose()
'''


def test_frozen_boot_is_exactly_046_and_live_upgrade_applies_only_047(tmp_path):
    database = tmp_path / 'frozen.db'
    result = run_frozen(RUNNER, FROZEN_BOOT, database)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(database))
    try:
        assert bootstrap_database(engine)['applied'] == [MIGRATION_047]
        first = bootstrap_database(engine)
        assert first['applied'] == []
        assert bootstrap_database(engine)['schema_fingerprint'] == first['schema_fingerprint']
        with engine.connect() as c:
            assert c.execute(text('SELECT tray_observation,do_not_feed,do_not_feed_note FROM filament_slots WHERE id=1')).one() == ('{"id":"kept"}', 0, None)
            assert c.execute(text('SELECT decision,spool_id FROM tray_confirmations WHERE slot_id=1')).one() == ('same', 1)
            assert c.execute(text('SELECT ace_status FROM printers WHERE id=1')).scalar_one() is None
    finally:
        engine.dispose()


def _package_copy(tmp_path):
    target = tmp_path / 'preserved046'
    shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path / 'preserved045').symlink_to(PACKAGE.parent / 'preserved045', target_is_directory=True)
    return target


@pytest.mark.parametrize('change,refusal', [
    ('manifest', 'manifest differs'),
    ('overlay_changed', 'overlay integrity differs'),
    ('overlay_missing', 'overlay inventory differs'),
    ('overlay_extra', 'overlay inventory differs'),
])
def test_tampered_package_is_refused(tmp_path, change, refusal):
    from core.schema.preserved046.integrity import check_package, Preserved046Error
    target = _package_copy(tmp_path)
    source = target / 'overlay/core/schema/migrations/046_tray_identity.py'
    if change == 'manifest':
        with (target / 'manifest.json').open('ab') as stream:
            stream.write(b'\n')
    elif change == 'overlay_changed':
        with source.open('ab') as stream:
            stream.write(b'\n# changed\n')
    elif change == 'overlay_missing':
        source.unlink()
    else:
        (target / 'overlay/core/extra_module.py').write_text('')
    with pytest.raises(Preserved046Error, match=refusal):
        check_package(target)


@pytest.mark.parametrize('field,value,refusal', [
    ('profile', 'restore-045', 'provenance differs'),
    ('source_commit', '6a3c6e17', 'provenance differs'),
    ('ledger_ids', sorted(BASE_MANIFEST['ledger_ids']), 'ledger differs'),
    ('ledger_ids', sorted(MANIFEST['ledger_ids'] + [MIGRATION_047]), 'ledger differs'),
    ('sql_paths', [], 'SQL inventory differs'),
])
def test_manifest_meaning_is_checked_independently_of_its_pin(field, value, refusal):
    from core.schema.preserved046.integrity import _check_metadata, Preserved046Error
    _check_metadata(MANIFEST, BASE_MANIFEST)
    with pytest.raises(Preserved046Error, match=refusal):
        _check_metadata({**MANIFEST, field: value}, BASE_MANIFEST)


TAMPERED_SOURCE = '''
target=bundle/'modules/system/backup_failure_alerts.py'
target.write_bytes(target.read_bytes()+b'# changed')
try:
    import modules.system.backup_failure_alerts
except Exception as error:
    print(type(error).__name__,error)
try:
    namespace['check_package']()
except Exception as error:
    print(type(error).__name__,error)
'''


def test_tampered_materialized_source_is_refused_at_import_and_recheck():
    result = run_frozen(RUNNER, TAMPERED_SOURCE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['Preserved046Error Preserved046 import source integrity differs',
                                          'Preserved046Error Preserved046 materialized source differs']


UNADMITTED = '''
for name in ('core.schema.preserved045','core.not_in_the_manifest','modules.system.not_in_the_manifest'):
    try:
        __import__(name)
    except Exception as error:
        print(type(error).__name__,error)
    else:
        print('admitted',name)
'''


def test_unadmitted_imports_are_refused():
    result = run_frozen(RUNNER, UNADMITTED)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        'Preserved046Error Preserved046 dependency is not admitted: core.schema.preserved045',
        'Preserved046Error Preserved046 dependency is not admitted: core.not_in_the_manifest',
        'Preserved046Error Preserved046 dependency is not admitted: modules.system.not_in_the_manifest']


def test_child_era_is_46_and_the_parent_accepts_only_46():
    tree = ast.parse(RUNNER.read_text())
    eras = [node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'era' for target in node.targets)
            and isinstance(node.value, ast.Constant)]
    assert eras == [46]
    assert preserved046._ACK.fullmatch(b'preserved046-validated:46\n')
    assert not preserved046._ACK.fullmatch(b'preserved046-validated:45\n')


@pytest.mark.parametrize('era,accepted', [(46, True), (45, False)])
def test_parent_acknowledgement_is_checked_against_a_real_child(tmp_path, monkeypatch, era, accepted):
    (tmp_path / 'runner.py').write_text(f"print('preserved046-validated:{era}',flush=True)\n")
    control = tmp_path / 'control.json'
    control.write_text('{}')
    monkeypatch.setattr(preserved046, '_BASE', tmp_path)
    deadline = time.monotonic() + 30
    if accepted:
        assert preserved046._bounded_child(control, deadline) == 46
    else:
        with pytest.raises(preserved046.Preserved046Error, match='did not validate'):
            preserved046._bounded_child(control, deadline)


EXIT_CODES = '''
from integrity import Preserved046Error
exit_code_for=namespace['exit_code_for']
print(exit_code_for(Preserved046Error('Preserved046 dependency is not admitted: modules.system.absent')))
print(exit_code_for(Preserved046Error('Preserved046 stage ledger differs')))
print(exit_code_for(Preserved046Error('Preserved046 stage tables differ')))
'''


def test_child_classifies_package_failures_as_environment_and_ledger_as_archive():
    result = run_frozen(RUNNER, EXIT_CODES)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ['78', '65', '65']


def _prepared(monkeypatch, tmp_path, failure):
    from unittest.mock import MagicMock
    from core import database_config
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: stage.PRESERVED046)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *a: ('unused', 'unused'))
    engine = MagicMock()
    engine.execution_options.return_value = engine
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *a, **k: engine)
    for name in ('sweep_stale', 'record', 'forget', 'drop_database'):
        monkeypatch.setattr(restore_artifacts, name, lambda *a, **k: None)
    def refuse(*a, **k):
        raise failure
    monkeypatch.setattr(preserved046, 'restore_into_stage', refuse)
    return stage.prepared_archive(archive, 'postgresql://localhost/target', 'postgresql://localhost/maintenance', None, None, tmp_path)


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved046 stage ledger differs: owned fixture', True),
    ('Preserved046 manifest differs', False),
])
def test_parent_side_errors_are_classified_by_what_they_judged(tmp_path, monkeypatch, message, archive_refusal):
    from modules.system import postgres_backup_service as backup
    expected = backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected, match=message):
        with _prepared(monkeypatch, tmp_path, preserved046.Preserved046Error(message)):
            pytest.fail('Invalid preserved stage must not be yielded')


@pytest.mark.parametrize('category,expected', [('archive', 'ArchiveRefused'), ('retry', 'PostgreSQLClientFailed'), ('environment', 'RestoreEnvironmentError')])
def test_child_refusal_category_selects_the_parent_failure(tmp_path, monkeypatch, category, expected):
    from modules.system import postgres_backup_service as backup
    refusal = preserved046.Preserved046Refused('Kind', 'Owned detail', category)
    with pytest.raises(getattr(backup, expected), match='Owned detail'):
        with _prepared(monkeypatch, tmp_path, refusal):
            pytest.fail('Refused preserved stage must not be yielded')


# Every era whose child directory a restore can leave behind, not only the newest two.
@pytest.mark.parametrize('era', ['035', '036', '037', '038', '039', '045', '046'])
def test_stale_work_and_confirmed_child_journal_are_swept(tmp_path, era):
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal / (f'.odin-preserved{era}-' + 'a' * 20)
    child.mkdir(mode=0o700)
    confirmed, intended = 'odin_validate_' + 'b' * 20, 'odin_validate_' + 'c' * 20
    (child / confirmed).write_text('')
    (child / (intended + '.intent')).write_text('')
    work = tmp_path / (f'.odin-preserved{era}-' + 'd' * 20)
    work.mkdir()
    unrelated = tmp_path / 'keep-me'
    unrelated.mkdir()
    old = time.time() - 7 * 3600
    for path in (child / confirmed, child / (intended + '.intent'), child, work, unrelated):
        os.utime(path, (old, old))
    asked = []
    result = artifacts.sweep_stale(tmp_path, 'unused', None, drop=lambda names: asked.extend(names) or names)
    assert asked == [confirmed] and result['databases'] == [confirmed]
    assert not child.exists() and not work.exists() and unrelated.exists()


def test_normal_cleanup_forgets_a_046_child_journal_entry(tmp_path):
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal / ('.odin-preserved046-' + 'a' * 20)
    child.mkdir(mode=0o700)
    name = 'odin_validate_' + 'b' * 20
    (child / name).write_text('')
    assert name in artifacts.journal_entries(tmp_path)
    artifacts.forget(tmp_path, name)
    assert not child.exists() and artifacts.journal_entries(tmp_path) == {}


def test_fresh_child_directories_are_kept(tmp_path):
    from modules.system import restore_artifacts as artifacts
    journal = artifacts.journal_dir(tmp_path)
    child = journal / ('.odin-preserved046-' + 'a' * 20)
    child.mkdir(mode=0o700)
    work = tmp_path / ('.odin-preserved046-' + 'd' * 20)
    work.mkdir()
    artifacts.sweep_stale(tmp_path, 'unused', None, drop=lambda names: pytest.fail('Nothing is stale'))
    assert child.exists() and work.exists()


def test_scanners_cover_the_frozen_overlay_only():
    root = freeze.ROOT
    assert '/backend/core/schema/preserved046/overlay/' in (root / '.semgrepignore').read_text().splitlines()
    gitleaks = (root / '.gitleaks.toml').read_text()
    assert r"'''^backend/core/schema/preserved046/manifest\.json$'''" in gitleaks
    assert 'preserved046/overlay' not in gitleaks
