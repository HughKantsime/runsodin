"""Compaction, bounded uploads and persistent failure notices (owned data only)."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from modules.system import backup_service as b
from tests.backup_restore.test_backup_service import _database, _marker


def test_bloated_current_database_wal_round_trip(tmp_path, monkeypatch):
    from core.schema import bootstrap_database
    live = tmp_path / 'live.db'
    engine = create_engine(f'sqlite:///{live}')
    bootstrap_database(engine)
    engine.dispose()
    with sqlite3.connect(live) as db:
        db.execute('CREATE TABLE compact_test(payload BLOB)')
        db.execute('INSERT INTO compact_test VALUES (zeroblob(?))', (105 * 1024 * 1024,))
        db.commit()
        db.execute('DELETE FROM compact_test')
        db.commit()
    assert live.stat().st_size > 100 * 1024 * 1024
    import shutil
    monkeypatch.setattr(b.shutil, 'disk_usage', lambda _: shutil._ntuple_diskusage(200 * 1024 * 1024, 136 * 1024 * 1024, 64 * 1024 * 1024))
    writer = sqlite3.connect(live)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute("INSERT INTO system_config(key,value) VALUES('compaction_marker','committed WAL')")
    writer.commit()
    url = f'sqlite:///{live}'
    try:
        candidate, metadata = b.create_online_backup(url)
        assert candidate.stat().st_size < live.stat().st_size
        assert b.validate_database(candidate) == metadata
        with sqlite3.connect(candidate) as db:
            assert db.execute("SELECT value FROM system_config WHERE key='compaction_marker'").fetchone() == ('committed WAL',)
    finally:
        writer.close()
    b.stage_restore(candidate, url)
    b.apply_pending_restore(url, pid_file=None)
    b.finalize_pending_restore(url, pid_file=None)
    b.validate_database(live)
    with sqlite3.connect(live) as db:
        assert db.execute("SELECT value FROM system_config WHERE key='compaction_marker'").fetchone() == ('committed WAL',)


def test_compacted_size_cap_and_cleanup(tmp_path, monkeypatch):
    live = tmp_path / 'live.db'
    _database(live, 'kept')
    monkeypatch.setattr(b, 'MAX_BACKUP_BYTES', 100)
    with pytest.raises(b.BackupValidationError, match='size'):
        b.create_online_backup(f'sqlite:///{live}')
    assert not list((tmp_path / 'backups').glob('*.db'))
    assert _marker(live) == 'kept'


@pytest.mark.parametrize('oversized', [False, True])
@pytest.mark.parametrize('postgres', [False, True])
def test_upload_is_bounded_and_cleans_temporary_file(tmp_path, monkeypatch, oversized, postgres):
    from modules.system import routes_backup as routes
    cap = 2 * 1024 * 1024
    monkeypatch.setattr(routes, 'MAX_BACKUP_BYTES', cap)
    from types import SimpleNamespace
    monkeypatch.setattr(routes, 'sql', SimpleNamespace(is_postgres=postgres))
    monkeypatch.setattr(routes, '_postgres_maintenance_config', lambda: ('postgresql://unused/odin', None, None))
    monkeypatch.setattr(routes.tempfile, 'tempdir', str(tmp_path))
    calls = []
    class Upload:
        filename = 'restore.dump' if postgres else 'restore.db'
        remaining = cap + (10 * cap if oversized else 0)
        async def read(self, count):
            calls.append(count)
            assert 0 < count <= 1024 * 1024
            size = min(count, self.remaining)
            self.remaining -= size
            return b'x' * size
    upload = Upload()
    def stage(path, *_args, **_kwargs):
        assert path.stat().st_size == cap
        return {}
    monkeypatch.setattr(routes, 'stage_restore', stage)
    monkeypatch.setattr(routes, 'stage_postgres_restore', stage)
    if oversized:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            asyncio.run(routes.restore_backup(upload, {'id': 1}, None))
        assert error.value.status_code == 413
        assert upload.remaining == 10 * cap - 1
    else:
        assert asyncio.run(routes.restore_backup(upload, {'id': 1}, None))['status'] == 'staged'
    assert not list(tmp_path.glob('odin-restore-upload-*'))


def test_failure_alert_once_per_reason_per_day_admin_only(tmp_path, owned_backup_alerts):
    from core.schema import bootstrap_database
    publish_backup_failure = owned_backup_alerts
    engine = create_engine(f'sqlite:///{tmp_path / "alerts.db"}')
    bootstrap_database(engine)
    factory = sessionmaker(bind=engine)
    with engine.begin() as db:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'admin','fixture','admin',1,NULL),(2,'viewer','fixture','viewer',1,NULL)"))
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    size = b.BackupValidationError('size outside range')
    disk = b.BackupValidationError('Insufficient free disk space')
    for error in (size, size, disk, disk):
        publish_backup_failure(error, session_factory=factory, now=now)
    with engine.begin() as db:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        rows = db.execute(text('SELECT user_id,message FROM alerts')).all()
        assert len(rows) == 2 and {row[0] for row in rows} == {1}
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        db.execute(text('UPDATE alerts SET is_read=1,is_dismissed=1'))
    publish_backup_failure(size, session_factory=factory, now=now + timedelta(hours=23))
    publish_backup_failure(size, session_factory=factory, now=now + timedelta(days=1, seconds=1))
    with engine.connect() as db:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        assert db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 3
    engine.dispose()


def test_automatic_backup_failure_preserves_error_and_publishes_once(monkeypatch):
    from modules.system import backup_failure_alerts as notices
    seen=[]
    monkeypatch.setattr(notices, 'publish_backup_failure', seen.append)
    error=b.BackupValidationError('Insufficient free disk space')
    @notices.report_backup_failure
    def safety_backup():
        raise error
    with pytest.raises(b.BackupValidationError) as caught:
        safety_backup()
    assert caught.value is error and seen == [error]


@pytest.mark.parametrize('error,reason', [
    (b.BackupValidationError('file size /private/path'), 'size'),
    (b.BackupValidationError('Insufficient free disk space /private/path'), 'disk space'),
    (b.BackupValidationError('invalid schema /private/path'), 'validation'),
    (OSError('cannot write /private/path'), 'snapshot'),
])
def test_manual_backup_error_names_reason_without_paths(monkeypatch,error,reason):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from modules.system import routes_backup as routes
    monkeypatch.setattr(routes, 'sql', SimpleNamespace(is_postgres=False))
    def refused(*args, **kwargs):
        raise error
    monkeypatch.setattr(routes, 'create_online_backup', refused)
    with pytest.raises(HTTPException) as caught:
        routes.create_backup({'id':1}, None)
    assert reason in caught.value.detail
    assert '/private/path' not in caught.value.detail
