"""Native sequence semantics plus client-free staged restore/rollback contracts."""
import pytest
from sqlalchemy import text

from core.schema import bootstrap_database
from modules.system import postgres_backup_service as backup
from modules.system.postgres_restore_identity import job_identity_floor, preserve_job_identity
from tests.test_contracts.test_controlled_postgres import native_pg


@pytest.mark.parametrize('previous,restored,expected', [
    ((15, True), (10, True), 16),
    ((10, True), (30, True), 31),
    ((1, False), (1, False), 1),
    ((1, True), (1, False), 2),
])
def test_postgres_identity_sequence_state(native_pg, previous, restored, expected):
    bootstrap_database(native_pg)
    with native_pg.begin() as db:
        db.execute(text("SELECT setval(pg_get_serial_sequence('jobs','id'),:value,:called)"),
                   {'value': restored[0], 'called': restored[1]})
        preserve_job_identity(db, previous)
        assert db.execute(text("INSERT INTO jobs(item_name) VALUES('next') RETURNING id")).scalar_one() == expected


@pytest.mark.parametrize('fail_after_restore', [False, True])
def test_staged_postgres_restore_keeps_pre_restore_sequence(tmp_path, monkeypatch, fail_after_restore):
    from tests.test_contracts._controlled_restore_clients import install
    state, calls, create_jobs, connection = install(monkeypatch, backup)
    # Prove the contract still runs when neither PostgreSQL client is installed.
    monkeypatch.setattr(backup.shutil, 'which', lambda executable: None)
    data = tmp_path / 'owned-data'; data.mkdir(mode=0o700)
    monkeypatch.setenv('ODIN_DATA_DIR', str(data))
    secret = tmp_path / 'app-password'; maintenance_secret = tmp_path / 'maintenance-password'
    for path in (secret, maintenance_secret):
        path.write_text('owned-contract-only'); path.chmod(0o600)
    target = 'postgresql://owned@localhost/controlled_restore'
    maintenance = 'postgresql://owned@localhost/postgres'
    options = dict(password_file=str(secret), maintenance_password_file=str(maintenance_secret), base_dir=data)
    assert create_jobs(10) == list(range(1, 11))
    candidate, _ = backup.create_postgres_backup(target, maintenance_url=maintenance, **options)
    state['jobs'] = [identifier for identifier in state['jobs'] if identifier < 6]
    staged = backup.stage_postgres_restore(candidate, target, maintenance, **options)
    # IDs issued after staging must be captured at offline apply, even if deleted.
    assert create_jobs(5) == list(range(11, 16))
    state['jobs'] = [identifier for identifier in state['jobs'] if identifier < 11]
    assert job_identity_floor(connection()) == (15, True)
    if fail_after_restore:
        with pytest.raises(RuntimeError, match='Injected post-restore failure'):
            backup.apply_pending_postgres_restore(target, maintenance,
                acknowledgement=staged['candidate_sha256'], fail_after_restore=True, **options)
    else:
        result = backup.apply_pending_postgres_restore(target, maintenance,
            acknowledgement=staged['candidate_sha256'], **options)
        assert result['status'] == 'restored'
    assert state['jobs'] == list(range(1, 6 if fail_after_restore else 11))
    assert create_jobs(1) == [16]
    assert create_jobs(1) == [17]
    expected_restores = 2 if fail_after_restore else 1
    assert [name for name, _ in calls].count('pg_dump') == 2
    assert [name for name, _ in calls].count('pg_restore') == expected_restores
    assert [floor for name, floor in calls if name == 'preserve'] == [(15, True)] * expected_restores
    assert not backup.postgres_paths(data).pending.exists()
    assert not backup.postgres_paths(data).manifest.exists()
