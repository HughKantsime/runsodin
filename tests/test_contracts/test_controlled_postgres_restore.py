"""Native PostgreSQL job sequences survive older staged backups and rollback."""
import shutil

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

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
def test_staged_postgres_restore_keeps_pre_restore_sequence(native_pg, tmp_path, monkeypatch, fail_after_restore):
    missing = [name for name in ('pg_dump', 'pg_restore') if not shutil.which(name)]
    if missing:
        pytest.skip('Required PostgreSQL clients unavailable: ' + ', '.join(missing))
    data = tmp_path / 'owned-data'; data.mkdir(mode=0o700)
    monkeypatch.setenv('ODIN_DATA_DIR', str(data))
    secret = tmp_path / 'app-password'; maintenance_secret = tmp_path / 'maintenance-password'
    for path in (secret, maintenance_secret):
        path.write_text(native_pg.url.password); path.chmod(0o600)
    maintenance = native_pg.url._replace(password=None).render_as_string(hide_password=False)
    with native_pg.connect().execution_options(isolation_level='AUTOCOMMIT') as db:
        db.exec_driver_sql('CREATE DATABASE controlled_restore')
    url = native_pg.url.set(database='controlled_restore')
    target = url._replace(password=None).render_as_string(hide_password=False)
    engine = create_engine(url, poolclass=NullPool)
    bootstrap_database(engine)
    def create_jobs(count):
        with engine.begin() as db:
            return [db.execute(text("INSERT INTO jobs(item_name) VALUES('part') RETURNING id")).scalar_one()
                    for _ in range(count)]
    options = dict(password_file=str(secret), maintenance_password_file=str(maintenance_secret), base_dir=data)
    try:
        assert create_jobs(10) == list(range(1, 11))
        candidate, _ = backup.create_postgres_backup(target, maintenance_url=maintenance, **options)
        with engine.begin() as db:
            db.execute(text('DELETE FROM jobs WHERE id>=6'))
        staged = backup.stage_postgres_restore(candidate, target, maintenance, **options)
        # IDs issued after staging must be captured at offline apply, even if deleted.
        assert create_jobs(5) == list(range(11, 16))
        with engine.begin() as db:
            db.execute(text('DELETE FROM jobs WHERE id>=11'))
            assert job_identity_floor(db) == (15, True)
        if fail_after_restore:
            with pytest.raises(RuntimeError, match='Injected post-restore failure'):
                backup.apply_pending_postgres_restore(target, maintenance,
                    acknowledgement=staged['candidate_sha256'], fail_after_restore=True, **options)
        else:
            result = backup.apply_pending_postgres_restore(target, maintenance,
                acknowledgement=staged['candidate_sha256'], **options)
            assert result['status'] == 'restored'
        assert create_jobs(1) == [16]
        bootstrap_database(engine)
        assert create_jobs(1) == [17]
    finally:
        engine.dispose()
        with native_pg.connect().execution_options(isolation_level='AUTOCOMMIT') as db:
            db.exec_driver_sql('DROP DATABASE controlled_restore WITH (FORCE)')
