"""cert-D ops: F18 Backup & Restore through the actual HTTP router and a real
canonical SQLite schema. Only the process boundary (restart) is simulated by
calling the real offline apply/finalize functions the startup path runs."""
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.cert_D_capture import publish

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/certDops-backup-wire.json'


def _marker(path):
    connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        row = connection.execute("SELECT value FROM system_config WHERE key='certDops_marker'").fetchone()
        return json.loads(row[0]) if row else None
    finally:
        connection.close()


@pytest.fixture
def backup_client(review_db, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_backup
    engine = review_db.get_bind()
    live = Path(engine.url.database).resolve()
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('certDops_marker',:v)"), {'v': json.dumps('before')})
    review_db.commit()
    monkeypatch.setattr(routes_backup.settings, 'database_url', f'sqlite:///{live}')
    monkeypatch.setattr(routes_backup, 'sql', SimpleNamespace(is_postgres=False))
    app = FastAPI()
    app.include_router(routes_backup.router)
    user = {'id': 3, 'role': 'admin', 'group_id': None, 'is_active': True}

    def request_db():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), user, live, routes_backup


def test_admin_creates_lists_downloads_and_deletes_point_in_time_snapshot(backup_client, review_db):
    client, _, live, _ = backup_client
    created = client.post('/backups')
    assert created.status_code == 200, created.text
    body = created.json()
    name = body['filename']
    assert name.startswith('odin_backup_') and name.endswith('.db')
    stored = live.parent / 'backups' / name
    assert stored.is_file() and stored.stat().st_mode & 0o777 == 0o600
    assert body['size_bytes'] == stored.stat().st_size
    # point-in-time: later live writes do not reach the snapshot
    review_db.execute(text("UPDATE system_config SET value=:v WHERE key='certDops_marker'"), {'v': json.dumps('after-snapshot')})
    review_db.commit()
    assert _marker(stored) == 'before' and _marker(live) == 'after-snapshot'
    listing = client.get('/backups')
    assert listing.status_code == 200
    assert [row['filename'] for row in listing.json()] == [name]
    download = client.get(f'/backups/{name}')
    assert download.status_code == 200
    assert download.headers['content-type'] == 'application/octet-stream'
    assert name in download.headers['content-disposition']
    assert download.content == stored.read_bytes()
    assert download.content[:16] == b'SQLite format 3\x00'
    audit = review_db.execute(text("SELECT details FROM audit_logs WHERE action='backup_created'")).scalar_one()
    assert name in str(audit)
    assert client.delete(f'/backups/{name}').status_code == 204
    assert not stored.exists() and client.get('/backups').json() == []
    fixture = {'scope': 'certDops owned HTTP producer: real POST/GET /backups on canonical SQLite schema',
               'create': body,
               'list': listing.json(),
               'download_prefix_hex': download.content[:16].hex()}
    publish(FIXTURE, fixture, key='backup', volatile={'filename'})  # odin_backup_<timestamp>.db


@pytest.mark.parametrize('role,group', [('operator', None), ('viewer', None), ('admin', 1)])
def test_backup_endpoints_are_admin_only(backup_client, role, group):
    client, user, live, _ = backup_client
    user.update(role=role, group_id=group)
    assert client.post('/backups').status_code == 403
    assert client.get('/backups').status_code == 403
    files = {'file': ('candidate.db', b'SQLite format 3\x00' + b'0' * 200, 'application/octet-stream')}
    assert client.post('/backups/restore', files=files).status_code == 403
    assert not (live.parent / 'backups').exists() or not list((live.parent / 'backups').iterdir())


def test_online_backup_uses_sqlite_backup_api_and_captures_uncheckpointed_wal(backup_client):
    client, _, live, _ = backup_client
    holder = sqlite3.connect(str(live))
    try:
        assert holder.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        holder.execute('PRAGMA wal_autocheckpoint=0')
        holder.execute("UPDATE system_config SET value=? WHERE key='certDops_marker'", (json.dumps('in-wal'),))
        holder.commit()
        wal = Path(str(live) + '-wal')
        assert wal.is_file() and wal.stat().st_size > 0
        # A naive file copy of the main database misses the committed WAL frame.
        naive = live.parent / 'naive-copy.db'
        shutil.copyfile(live, naive)
        assert _marker(naive) == 'before'
        created = client.post('/backups')
        assert created.status_code == 200, created.text
        backup = live.parent / 'backups' / created.json()['filename']
        # The downloadable file alone (no sidecars) must hold the WAL-committed row.
        download = client.get(f"/backups/{backup.name}")
        assert download.status_code == 200
        isolated = live.parent / 'isolated'
        isolated.mkdir()
        alone = isolated / 'downloaded.db'
        alone.write_bytes(download.content)
        assert _marker(alone) == 'in-wal'
        connection = sqlite3.connect(str(alone))
        try:
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        finally:
            connection.close()
    finally:
        holder.close()


def test_ui_restore_upload_stages_db_with_safety_backup_then_restart_overwrites(backup_client, review_db, tmp_path):
    from modules.system.backup_service import apply_pending_restore, finalize_pending_restore
    client, _, live, routes_backup = backup_client
    candidate = tmp_path / 'candidate.db'
    source = sqlite3.connect(str(live)); target = sqlite3.connect(str(candidate))
    source.backup(target); source.close()
    target.execute("UPDATE system_config SET value=? WHERE key='certDops_marker'", (json.dumps('restored'),))
    target.commit(); target.close()
    response = client.post('/backups/restore', files={'file': ('candidate.db', candidate.read_bytes(), 'application/octet-stream')})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'staged' and body['restart_required'] is True
    safety = live.parent / 'backups' / body['pre_restore_backup']
    assert body['pre_restore_backup'].startswith('pre_restore_') and safety.is_file()
    assert _marker(safety) == 'before'
    # Staging never overwrites the running database.
    assert _marker(live) == 'before'
    review_db.close(); review_db.get_bind().dispose()
    url = routes_backup.settings.database_url
    applied = apply_pending_restore(url, pid_file=None)
    assert applied and applied['status'] == 'applied_pending_validation'
    finished = finalize_pending_restore(url, pid_file=None)
    assert finished and finished['status'] == 'restored'
    assert _marker(live) == 'restored'
    assert _marker(safety) == 'before'
    publish(FIXTURE, {k: body[k] for k in ('status', 'message', 'restart_required', 'pre_restore_backup')}, key='restore',
            volatile={'pre_restore_backup'})  # pre_restore_<timestamp>.db


def test_restore_refuses_non_db_and_invalid_upload_without_staging(backup_client):
    client, _, live, _ = backup_client
    refused = client.post('/backups/restore', files={'file': ('candidate.dump', b'x' * 200, 'application/octet-stream')})
    assert refused.status_code == 400 and '.db' in refused.json()['detail']
    corrupt = client.post('/backups/restore', files={'file': ('candidate.db', b'not sqlite' * 50, 'application/octet-stream')})
    assert corrupt.status_code == 400
    assert not (live.parent / 'restore-pending.db').exists()
    assert _marker(live) == 'before'


def test_postgres_deployment_restore_requires_dump_not_db(backup_client, monkeypatch):
    """Claim text says '.db'; PostgreSQL deployments only accept pg_dump '.dump'."""
    client, _, _, routes_backup = backup_client
    monkeypatch.setattr(routes_backup, 'sql', SimpleNamespace(is_postgres=True))
    refused = client.post('/backups/restore', files={'file': ('candidate.db', b'SQLite format 3\x00' + b'0' * 200, 'application/octet-stream')})
    assert refused.status_code == 400
    assert refused.json()['detail'] == 'Only .dump files are supported for this database'
