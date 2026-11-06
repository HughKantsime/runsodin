"""Older backups cannot reissue job IDs exported after the backup was taken."""
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.schema import bootstrap_database
from core.job_identity import insert_job
from modules.system import backup_service as backup


def create_jobs(path, count):
    with sqlite3.connect(path) as db:
        ids = [insert_job(db, 'INSERT INTO jobs (item_name) VALUES (?)', ('part',)).lastrowid
               for _ in range(count)]
    return ids


@pytest.mark.parametrize('backup_shape', ['current', 'pre055'])
@pytest.mark.parametrize('extra_floor', ['none', 'restored_high_water', 'restored_reference'])
def test_restore_keeps_all_time_job_identity(tmp_path, backup_shape, extra_floor):
    path = tmp_path / 'live.db'
    url = 'sqlite:///' + str(path)
    engine = create_engine(url)
    bootstrap_database(engine)
    engine.dispose()
    assert create_jobs(path, 10) == list(range(1, 11))
    candidate, _ = backup.create_online_backup(url)
    with sqlite3.connect(candidate) as db:
        if backup_shape == 'pre055':
            for name in ('jobs_high_water_insert', 'jobs_high_water_delete'):
                db.execute('DROP TRIGGER ' + name)
            db.execute('DROP TABLE job_id_high_water')
            db.execute("DELETE FROM odin_schema_migrations WHERE migration_id='python:055-controlled-history'")
        if extra_floor == 'restored_high_water' and backup_shape == 'current':
            db.execute('UPDATE job_id_high_water SET high_water=30')
        elif extra_floor != 'none':
            db.execute('CREATE TABLE legacy_export(job_id INTEGER)')
            db.execute('INSERT INTO legacy_export VALUES(40)')
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM jobs WHERE id>=6')
    # Stage before issuing more IDs: the floor must be read at offline apply.
    backup.stage_restore(candidate, url)
    assert create_jobs(path, 5) == list(range(11, 16))
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM jobs WHERE id>=11')
    assert backup.apply_pending_restore(url, pid_file=None)['status'] == 'applied_pending_validation'
    engine = create_engine(url)
    bootstrap_database(engine)
    engine.dispose()
    backup.finalize_pending_restore(url, pid_file=None)
    floor = 15 if extra_floor == 'none' else 30 if extra_floor == 'restored_high_water' and backup_shape == 'current' else 40
    assert create_jobs(path, 1) == [floor + 1]
    # Restart + ORM insertion must share the same restored allocator state.
    engine = create_engine(url)
    bootstrap_database(engine)
    from modules.jobs.models import Job
    with Session(engine) as db:
        job = Job(item_name='after restart')
        db.add(job); db.flush()
        assert job.id == floor + 2
        db.commit()
    engine.dispose()


def test_identity_merge_failure_rolls_back_original_database(tmp_path, monkeypatch):
    from modules.system import sqlite_restore_identity
    path = tmp_path / 'live.db'
    url = 'sqlite:///' + str(path)
    engine = create_engine(url); bootstrap_database(engine); engine.dispose()
    create_jobs(path, 10)
    candidate, _ = backup.create_online_backup(url)
    create_jobs(path, 5)
    backup.stage_restore(candidate, url)
    def fail(*args):
        raise RuntimeError('identity merge failed')
    monkeypatch.setattr(sqlite_restore_identity, 'preserve_job_identity', fail)
    with pytest.raises(RuntimeError, match='identity merge failed'):
        backup.apply_pending_restore(url, pid_file=None)
    assert create_jobs(path, 1) == [16]
    assert not backup.paths_from_database_url(url).manifest.exists()
