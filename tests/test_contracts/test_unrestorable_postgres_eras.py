"""Unsupported historical archives fail before staging and remain recoverable."""
import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from modules.system import pre026_restore_stage as stage, postgres_backup_service as backup

ROOT = Path(__file__).resolve().parents[2]
RANGES = {
    39: '2.0.16 to 2.0.17', 40: '2.0.18 to 2.0.23', 43: '2.0.24 to 2.0.26',
    44: '2.0.27 to 2.0.28', 45: '2.0.29 to 2.0.30', 46: '2.0.31 to 2.0.44',
}
GUIDE_TITLE = 'PostgreSQL backups from 2.0.16 to 2.0.44'


def refusal_text(era):
    return (f'This backup was made by ODIN {RANGES[era]} on PostgreSQL. '
            'This ODIN version cannot read it. Restore it on the ODIN version that made it, '
            'upgrade that install to this version, then take a new backup. '
            'Or keep running your current install and take a new backup. Nothing was changed. '
            f'See "{GUIDE_TITLE}" in the backup guide.')


@pytest.mark.parametrize('era', stage.UNRESTORABLE_POSTGRES_ERAS)
def test_plain_refusal_precedes_credentials_database_and_artifacts(tmp_path, monkeypatch, era):
    from core import database_config
    from modules.system import restore_artifacts
    archive = tmp_path / 'owned.dump'; archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: getattr(stage, f'PRESERVED{era:03d}'))
    connect = Mock(side_effect=AssertionError('No database may be opened'))
    credentials = Mock(side_effect=AssertionError('Credentials must not mask the refusal'))
    record = Mock(side_effect=AssertionError('No restore artifact may be recorded'))
    monkeypatch.setattr(database_config, 'create_database_engine', connect)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', credentials)
    monkeypatch.setattr(restore_artifacts, 'record', record)
    with pytest.raises(backup.UnsupportedHistoricalArchive) as caught:
        with stage.prepared_archive(archive, 'unused', 'unused', None, None, tmp_path):
            pytest.fail('Unsupported archive yielded')
    assert str(caught.value) == refusal_text(era)
    assert backup._as_archive_refusal(caught.value) is caught.value
    connect.assert_not_called(); credentials.assert_not_called(); record.assert_not_called()
    assert archive.read_bytes() == b'owned fixture'
    assert list(tmp_path.iterdir()) == [archive]


def test_release_ranges_are_complete_and_follow_recorded_ledgers():
    releases = json.loads(stage._RELEASE_LEDGERS.read_text())['releases']
    assert set(RANGES) == set(stage.UNRESTORABLE_POSTGRES_ERAS) == set(stage._CONTRACT_ERAS.values())
    for era, expected in RANGES.items():
        bundle = import_module(f'core.schema.preserved{era:03d}')
        post027 = bundle.ledger_ids() & set(stage.post027_migration_ids())
        versions = sorted((v for v, ids in releases.items() if set(ids) == post027),
                          key=lambda v: tuple(map(int, v.split('.'))))
        assert versions and f'{versions[0]} to {versions[-1]}' == expected
        assert stage.historical_release_range(era) == expected


def test_era_without_a_recorded_release_fails(tmp_path, monkeypatch):
    ledger = tmp_path / 'ledgers.json'; ledger.write_text('{"releases": {"2.0.0": []}}')
    monkeypatch.setattr(stage, '_RELEASE_LEDGERS', ledger)
    with pytest.raises(backup.RestoreEnvironmentError, match='missing backup compatibility information'):
        stage.historical_release_range(39)


@pytest.mark.parametrize('era', [38, 53])
def test_supported_historical_eras_reach_the_existing_stage(tmp_path, monkeypatch, era):
    from core import database_config
    from modules.system import restore_artifacts
    bundle = import_module(f'core.schema.preserved{era:03d}')
    archive = tmp_path / 'owned.dump'; archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: getattr(stage, f'PRESERVED{era:03d}'))
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    monkeypatch.setattr(backup, '_maintenance_urls', lambda *a: ('unused', 'unused'))
    engine = MagicMock(); engine.execution_options.return_value = engine
    monkeypatch.setattr(database_config, 'create_database_engine', lambda *a, **k: engine)
    for name in ('sweep_stale', 'record', 'forget', 'drop_database'):
        monkeypatch.setattr(restore_artifacts, name, lambda *a, **k: None)
    error_type = getattr(bundle, f'Preserved{era:03d}Error')
    child = Mock(side_effect=error_type(f'Preserved{era:03d} stage ledger differs: owned fixture'))
    monkeypatch.setattr(bundle, 'restore_into_stage', child)
    with pytest.raises(backup.ArchiveRefused, match='stage ledger differs'):
        with stage.prepared_archive(archive, 'unused', 'postgresql://localhost/maintenance', None, None, tmp_path):
            pytest.fail('Invalid stage yielded')
    child.assert_called_once()


def test_current_archive_is_yielded_unchanged(tmp_path, monkeypatch):
    archive = tmp_path / 'owned.dump'; archive.write_bytes(b'owned fixture')
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: None)
    with stage.prepared_archive(archive, 'unused', 'unused', None, None, tmp_path) as prepared:
        assert prepared == archive


@pytest.mark.parametrize('era', stage.UNRESTORABLE_POSTGRES_ERAS)
@pytest.mark.parametrize('route', ['upload', 'listed'])
def test_both_routes_return_the_full_refusal_without_staging(tmp_path, monkeypatch, era, route):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import controlled_exports
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_backup, restore_artifacts
    monkeypatch.setenv('ODIN_DATA_DIR', str(tmp_path))
    paths = backup.postgres_paths(); paths.backups.mkdir()
    archive = paths.backups / 'odin_backup_owned.dump'; archive.write_bytes(b'x' * 200)
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: getattr(stage, f'PRESERVED{era:03d}'))
    monkeypatch.setattr(restore_artifacts, 'sweep_stale', lambda *a, **k: None)
    monkeypatch.setattr(controlled_exports, 'acknowledge_export', lambda *a, **k: None)
    monkeypatch.setattr(routes_backup, 'sql', SimpleNamespace(is_postgres=True))
    monkeypatch.setattr(routes_backup, '_postgres_maintenance_config', lambda: ('unused', None, None))
    db = MagicMock()
    app = FastAPI(); app.include_router(routes_backup.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None, '_auth_kind': 'session'}
    with TestClient(app) as client:
        response = (client.post('/api/backups/restore', files={'file': ('owned.dump', b'x' * 200)})
                    if route == 'upload' else client.post(f'/api/backups/{archive.name}/stage'))
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': refusal_text(era)}
    assert not paths.pending.exists() and not paths.manifest.exists()
    assert archive.read_bytes() == b'x' * 200
    db.commit.assert_not_called()


@pytest.mark.parametrize('era', stage.UNRESTORABLE_POSTGRES_ERAS)
def test_coordinator_keeps_pending_archive_and_manifest_without_quarantine(tmp_path, monkeypatch, era):
    from modules.system import restore_coordinator as coordinator
    from tests.test_contracts.test_postgres_restore_boot_guard import _stage
    paths = _stage(tmp_path)
    before = (paths.pending.read_bytes(), paths.manifest.read_bytes())
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: getattr(stage, f'PRESERVED{era:03d}'))
    def apply(*a, **k):
        with backup._archive_phase(stage.prepared_archive(paths.pending, 'unused', 'unused', None, None, paths.backups)):
            pytest.fail('Unsupported archive applied')
    quarantine = Mock(side_effect=AssertionError('A usable historical archive must not be quarantined'))
    sleep = Mock(side_effect=AssertionError('An unsupported era must not be retried'))
    monkeypatch.setattr(coordinator, 'apply_pending_postgres_restore', apply)
    monkeypatch.setattr(coordinator, 'quarantine_failed_pending_postgres_restore', quarantine)
    monkeypatch.setattr(coordinator.time, 'sleep', sleep)
    with pytest.raises(backup.UnsupportedHistoricalArchive) as caught:
        coordinator._apply_postgres_with_retry('unused', 'unused', None, None)
    assert str(caught.value) == refusal_text(era)
    assert (paths.pending.read_bytes(), paths.manifest.read_bytes()) == before
    quarantine.assert_not_called(); sleep.assert_not_called()
    assert list(paths.backups.iterdir()) == []


def test_refusal_links_to_documented_recovery():
    runbook = (ROOT / 'docs/BACKUP_RESTORE_RUNBOOK.md').read_text()
    assert f'## {GUIDE_TITLE}\n' in runbook
    assert 'restore-pending-postgres.json' in runbook and 'restore_in_progress' in runbook


def test_offline_apply_reaches_refusal_before_replacing_target(tmp_path, monkeypatch):
    from modules.system import restore_coordinator as coordinator
    from tests.test_contracts.test_postgres_restore_boot_guard import _stage
    paths = _stage(tmp_path)
    manifest = json.loads(paths.manifest.read_text())
    manifest['sha256'] = backup._sha256(paths.pending)
    paths.manifest.write_text(json.dumps(manifest))
    before = (paths.pending.read_bytes(), paths.manifest.read_bytes())
    monkeypatch.setenv('ODIN_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('ODIN_POSTGRES_RESTORE_ACKNOWLEDGEMENT', manifest['sha256'])
    monkeypatch.setattr(stage, 'identify_contract', lambda *a: stage.PRESERVED046)
    monkeypatch.setattr(backup, '_require_distinct_maintenance_secret', lambda *a: None)
    engine = MagicMock()
    monkeypatch.setattr(backup, 'create_database_engine', lambda *a, **k: engine)
    monkeypatch.setattr(backup, '_assert_no_target_sessions', lambda *a, **k: None)
    monkeypatch.setattr(backup, '_assert_no_external_dependencies', lambda *a: None)
    rollback = paths.backups / 'rollback_owned.dump'; rollback.write_bytes(b'current data')
    monkeypatch.setattr(backup, 'create_postgres_backup', lambda *a, **k: (rollback, {}))
    restore = Mock(side_effect=AssertionError('The target must not be replaced'))
    quarantine = Mock(side_effect=AssertionError('The archive must not be quarantined'))
    monkeypatch.setattr(backup, '_restore_postgres_archive', restore)
    monkeypatch.setattr(coordinator, 'quarantine_failed_pending_postgres_restore', quarantine)
    with pytest.raises(backup.UnsupportedHistoricalArchive) as caught:
        coordinator._apply_postgres_with_retry('postgresql://localhost/target', 'postgresql://localhost/maintenance', None, None)
    assert str(caught.value) == refusal_text(46)
    assert (paths.pending.read_bytes(), paths.manifest.read_bytes()) == before
    assert not rollback.exists()  # Existing apply cleanup removes its unused rollback dump.
    restore.assert_not_called(); quarantine.assert_not_called()
