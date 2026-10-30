"""preserved045 had no dedicated contract. It is frozen, including its known restore defect."""
import ast
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import create_engine, inspect, text

from core.schema import bootstrap_database, preserved044, preserved045

from .test_preserved046_bundle import RESTORE_CLOSURE, run_frozen

MIGRATION_045 = 'python:045-pause-cancel-alerts'
PACKAGE = Path(preserved045.__file__).parent
RUNNER = PACKAGE / 'runner.py'

FROZEN_BOOT = '''
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
from core.base import AlertType
assert 'print_paused' in [t.value for t in AlertType]
engine=create_engine('sqlite:///'+sys.argv[2])
assert 'python:045-pause-cancel-alerts' in bootstrap_database(engine)['applied']
assert bootstrap_database(engine)['applied']==[]
with engine.connect() as c:
    assert {r[0] for r in c.execute(text('SELECT migration_id FROM odin_schema_migrations'))}==set(manifest['ledger_ids'])
assert 'tray_confirmations' not in inspect(engine).get_table_names()
engine.dispose()
'''


def test_frozen_boot_repeat_and_live_upgrade(tmp_path):
    database = tmp_path / 'frozen.db'
    result = run_frozen(RUNNER, FROZEN_BOOT, database)
    assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(database))
    try:
        assert bootstrap_database(engine)['applied'] == ['python:046-tray-identity', 'python:047-slot-feed-policy']
        assert bootstrap_database(engine)['applied'] == []
        assert 'tray_confirmations' in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize('version', ['2.0.29', '2.0.30'])
def test_exact_released_ledgers_route_here(tmp_path, monkeypatch, version):
    from core.schema import preserved029
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup
    ids = preserved045.ledger_ids()
    assert ids == preserved044.ledger_ids() | {MIGRATION_045}
    releases = json.loads(Path('backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert ids == preserved029.ledger_ids() | set(releases[version])
    assert 'python:046-tray-identity' not in ids
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'x' * 100)
    monkeypatch.setattr(backup, '_run_pg_tool', lambda *a: '')
    monkeypatch.setattr(backup, '_toc_inventory', lambda *a: None)
    monkeypatch.setattr(stage, 'archive_ledger_ids', lambda *a: ids)
    assert stage.identify_contract(archive, None, None, tmp_path) is stage.PRESERVED045


@pytest.mark.parametrize('changed', ['manifest.json', 'overlay/core/schema/migrations/045_pause_cancel_alerts.py'])
def test_tamper_refused(tmp_path, changed):
    from core.schema.preserved045.integrity import check_package, Preserved045Error
    target = tmp_path / 'preserved045'
    shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path / 'preserved044').symlink_to(PACKAGE.parent / 'preserved044', target_is_directory=True)
    with (target / changed).open('ab') as stream:
        stream.write(b'\n# changed\n')
    with pytest.raises(Preserved045Error, match='differs'):
        check_package(target)


def test_child_era_is_accepted_by_parent():
    tree = ast.parse(RUNNER.read_text())
    eras = [node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'era' for target in node.targets)
            and isinstance(node.value, ast.Constant)]
    assert eras == [45]
    assert preserved045._ACK.fullmatch(b'preserved045-validated:45\n')


@pytest.mark.parametrize('message,archive_refusal', [
    ('Preserved045 stage ledger differs: owned fixture', True),
    ('Preserved045 package hash differs', False),
])
def test_parent_side_errors_are_classified(tmp_path, monkeypatch, message, archive_refusal):
    from unittest.mock import MagicMock
    from core import database_config
    from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup, restore_artifacts
    archive = tmp_path / 'owned.dump'
    archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: stage.PRESERVED045)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *a: ('unused', 'unused'))
    engine = MagicMock()
    engine.execution_options.return_value = engine
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *a, **k: engine)
    for name in ('sweep_stale', 'record', 'forget', 'drop_database'):
        monkeypatch.setattr(restore_artifacts, name, lambda *a, **k: None)
    def refuse(*a, **k):
        raise preserved045.Preserved045Error(message)
    monkeypatch.setattr(preserved045, 'restore_into_stage', refuse)
    expected = backup.ArchiveRefused if archive_refusal else backup.RestoreEnvironmentError
    with pytest.raises(expected, match=message):
        with stage.prepared_archive(archive, 'postgresql://localhost/target', 'postgresql://localhost/maintenance', None, None, tmp_path):
            pytest.fail('Invalid preserved stage must not be yielded')


def test_known_defect_postgres_restore_cannot_import_its_backup_service():
    """The frozen 2.0.29/2.0.30 manifest omits a module its frozen backup service imports.

    A restore of those PostgreSQL archives stops with exit 78 before the target is touched.
    The bundle is byte-pinned, so this needs its own repair; this test fails when that lands.
    """
    result = run_frozen(RUNNER, RESTORE_CLOSURE)
    assert result.returncode != 0
    assert 'Preserved045 dependency is not admitted: modules.system.backup_failure_alerts' in result.stderr
