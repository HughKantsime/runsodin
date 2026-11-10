"""Legacy download bytes, asynchronous scrubbing and truthful export audit."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest
from fastapi import HTTPException

from modules.system import backup_api_export as exports
from modules.system import routes_backup as routes


@pytest.mark.parametrize('body', [b'PGDMPopaque archive', b'SQLite format 3\0legacy', b'-- plain SQL', b'\x1f\x8bgzip', b'custom-format'])
def test_flags_off_all_formats_identical(tmp_path, monkeypatch, body):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', raising=False)
    source = tmp_path / 'odin_backup_test.db'
    source.write_bytes(body)
    monkeypatch.setattr(exports.subprocess, 'run', lambda *a, **kw: pytest.fail('flags off inspected PG'))
    monkeypatch.setattr(exports.shutil, 'copyfile', lambda *a, **kw: pytest.fail('flags off copied DB'))
    exported, cleanup = exports.prepare_export(source)
    assert exported == source and cleanup is None
    response = exports.download_response(source)
    assert Path(response.path).read_bytes() == body
    assert response.background is None


@pytest.mark.parametrize('body', [b'-- SQL', b'\x1f\x8bgzip', b'opaque'])
def test_enabled_scrub_leaves_unknown_formats_alone(tmp_path, monkeypatch, body):
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    source = tmp_path / 'backup.dump'
    source.write_bytes(body)
    assert exports.prepare_export(source) == (source, None)
    assert source.read_bytes() == body


def test_sqlite_preparation_background_and_success_only_audit(tmp_path, monkeypatch):
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    source = tmp_path / 'odin_backup_test.db'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE system_config(key TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO system_config VALUES('license_lock_evidence', 'private-evidence')")
        db.commit()
    entered, release = threading.Event(), threading.Event()
    original = exports.prepare_export
    request_thread = threading.get_ident()
    def prepare(path):
        assert threading.get_ident() != request_thread
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(exports, 'prepare_export', prepare)
    monkeypatch.setattr(routes, '_safe_backup_path', lambda name: source)
    from core import controlled_exports
    monkeypatch.setattr(controlled_exports, 'acknowledge_export', lambda *a: None)
    audit = []
    monkeypatch.setattr(routes, 'audit_download', lambda *a, **kw: audit.append(a[1]))
    try:
        response = routes._download_backup(source.name, {}, object())
        assert response.status_code == 202 and response.headers['retry-after'] == '2'
        assert entered.wait(5)
        assert audit == []
        assert routes._download_backup(source.name, {}, object()).status_code == 202
        release.set()
        with exports._jobs_lock:
            job, timer = next(iter(exports._jobs.values()))
        job.result(timeout=10)
        response = routes._download_backup(source.name, {}, object())
        assert response.status_code == 200 and audit == ['backup.downloaded']
        assert not exports._jobs and timer.finished.is_set()
        assert b'private-evidence' not in Path(response.path).read_bytes()
        assert b'private-evidence' in source.read_bytes()
        asyncio.run(response.background())
        assert not Path(response.path).exists()
    finally:
        release.set()


@pytest.mark.parametrize('failure', [409, 503])
def test_failed_export_never_records_download(tmp_path, monkeypatch, failure):
    source = tmp_path / 'odin_backup_test.dump'
    source.write_bytes(b'PGDMP')
    monkeypatch.setattr(routes, '_safe_backup_path', lambda name: source)
    from core import controlled_exports
    monkeypatch.setattr(controlled_exports, 'acknowledge_export', lambda *a: None)
    monkeypatch.setattr(routes, 'audit_download', lambda *a, **kw: pytest.fail('refusal audited as downloaded'))
    def refuse(*args): raise HTTPException(failure, 'export refused')
    monkeypatch.setattr(exports, 'download_response', refuse)
    with pytest.raises(HTTPException) as exc:
        routes._download_backup(source.name, {}, object())
    assert exc.value.status_code == failure


def test_missing_bundle_companion_never_records_download(tmp_path, monkeypatch):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', raising=False)
    source = tmp_path / 'odin_backup_test.db'
    source.write_bytes(b'legacy-format')
    monkeypatch.setattr(routes, '_safe_backup_path', lambda name: source)
    from core import controlled_exports
    monkeypatch.setattr(controlled_exports, 'acknowledge_export', lambda *a: None)
    monkeypatch.setattr(routes, 'audit_download', lambda *a, **kw: pytest.fail('failed bundle audited'))
    with pytest.raises(HTTPException) as exc:
        routes._download_backup(source.name, {}, object(), bundle=True)
    assert exc.value.status_code == 409
