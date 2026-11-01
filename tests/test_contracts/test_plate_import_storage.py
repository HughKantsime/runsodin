"""Storage limits, stale staging and uncertain transactions."""
import fcntl
import os
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from tests.test_contracts.test_plate_import import importer, review_db, token_client, upload  # noqa: F401


def test_sweep_only_expired_unlocked_staging(tmp_path):
    from modules.models_library.routes import plate_imports
    old = tmp_path / '.plate-import-old'
    active = tmp_path / '.plate-import-active'
    fresh = tmp_path / '.plate-import-fresh'
    final = tmp_path / 'final.gcode'
    outside = tmp_path / 'outside'
    for path in (old, active, fresh, outside):
        path.mkdir()
        (path / 'payload').write_bytes(b'fixture')
    final.write_bytes(b'final')
    link = tmp_path / '.plate-import-link'
    link.symlink_to(outside, target_is_directory=True)
    expired = time.time() - 90000
    for path in (old, active, outside):
        os.utime(path, (expired, expired))
    fd = os.open(active, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        plate_imports._sweep_staging(tmp_path)
    finally:
        os.close(fd)
    assert not old.exists()
    assert all(path.exists() for path in (active, fresh, final, outside, link))


def test_low_space_refuses_before_upload_or_analysis(importer, monkeypatch):
    from modules.models_library.routes import plate_imports
    monkeypatch.setattr(plate_imports.shutil, 'disk_usage', lambda root: SimpleNamespace(free=plate_imports.IMPORT_REQUIRED_FREE_BYTES - 1))
    response = upload(importer)
    assert response.status_code == 507, response.text
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('change', ["is_active=0", "api_type='bambu'"])
def test_target_refused_before_analysis(importer, review_db, monkeypatch, change):
    from modules.models_library.routes import plate_imports
    review_db.execute(text('UPDATE printers SET ' + change + ' WHERE id=9'))
    review_db.commit()
    async def forbidden(*args):
        raise AssertionError('analysis must not run')
    monkeypatch.setattr(plate_imports.mesh_isolation, 'import_plates_isolated', forbidden)
    response = upload(importer)
    assert response.status_code == 400, response.text
    assert not list(importer[2].iterdir())


@pytest.mark.parametrize('rollback_failed', [False, True])
def test_uncertain_unlanded_commit_retains_whole_batch_and_ids(importer, review_db, monkeypatch, rollback_failed, caplog):
    from modules.models_library.routes import plate_imports
    original = plate_imports._persist
    reserved = []
    def persist(db, *args):
        commit, rollback = db.commit, db.rollback
        def fail_commit():
            reserved.extend(db.execute(text('SELECT id FROM print_files WHERE stored_path LIKE :path'), {'path': str(importer[2]) + '/%'}).scalars())
            raise OSError('private commit detail')
        def fail_rollback():
            raise OSError('private rollback detail')
        monkeypatch.setattr(db, 'commit', fail_commit)
        if rollback_failed:
            monkeypatch.setattr(db, 'rollback', fail_rollback)
        try:
            return original(db, *args)
        finally:
            monkeypatch.setattr(db, 'commit', commit)
            monkeypatch.setattr(db, 'rollback', rollback)
    monkeypatch.setattr(plate_imports, '_persist', persist)
    response = upload(importer)
    assert response.status_code == 500, response.text
    assert len(reserved) == 3
    assert all(str(file_id) in response.json()['detail'] and str(file_id) in caplog.text for file_id in reserved)
    assert 'private' not in response.text
    assert not list(importer[2].glob('.plate-import-*'))
    payloads = list(importer[2].rglob('*.gcode'))
    assert len(payloads) == 3
    assert {path.read_bytes() for path in payloads} == {f'; synthetic plate {number}\nG28\n'.encode() for number in (1, 4, 10)}


@pytest.mark.parametrize('boundary', ['is_dir', 'open', 'flock', 'rmtree'])
def test_cleanup_permission_errors_do_not_abort_import(importer, monkeypatch, caplog, boundary):
    from pathlib import Path
    from modules.models_library.routes import plate_imports
    root = importer[2]
    bad = root / '.plate-import-inaccessible'
    bad.mkdir()
    (bad / 'payload').write_bytes(b'fixture')
    expired = time.time() - 90000
    os.utime(bad, (expired, expired))
    def denied():
        raise PermissionError('private filesystem details')
    if boundary == 'is_dir':
        original = os.scandir
        class Entry:
            def __init__(self, entry):
                self.entry = entry
            def __getattr__(self, name):
                return getattr(self.entry, name)
            def is_dir(self, **kwargs):
                if self.entry.name == bad.name:
                    denied()
                return self.entry.is_dir(**kwargs)
        class Entries:
            def __enter__(self):
                self.scan = original(root)
                return iter(Entry(entry) for entry in self.scan)
            def __exit__(self, *args):
                self.scan.close()
        monkeypatch.setattr(os, 'scandir', lambda path: Entries() if path == root else original(path))
    elif boundary == 'open':
        original = os.open
        def open_path(path, *args, **kwargs):
            if str(path) == str(bad):
                denied()
            return original(path, *args, **kwargs)
        monkeypatch.setattr(os, 'open', open_path)
    elif boundary == 'flock':
        original = fcntl.flock
        def lock(fd, *args):
            if Path(f'/proc/self/fd/{fd}').resolve() == bad:
                denied()
            return original(fd, *args)
        monkeypatch.setattr(fcntl, 'flock', lock)
    else:
        original = plate_imports.shutil.rmtree
        def remove(path, *args, **kwargs):
            if str(path) == str(bad):
                denied()
            return original(path, *args, **kwargs)
        monkeypatch.setattr(plate_imports.shutil, 'rmtree', remove)
    response = upload(importer)
    assert response.status_code == 200, response.text
    assert bad.exists()
    assert 'cleanup skipped (PermissionError)' in caplog.text
    assert 'private filesystem details' not in caplog.text


@pytest.mark.parametrize('failed_rescue', [False, True])
def test_pre_move_failure_and_failed_rollback_reports_ids_and_attempts_all_rescues(
    importer, review_db, monkeypatch, caplog, failed_rescue,
):
    from modules.models_library.routes import plate_imports
    original = plate_imports._persist
    replace = plate_imports.os.replace
    reserved, attempts = [], []
    def persist(db, *args):
        flush, rollback = db.flush, db.rollback
        def fail_flush(*args, **kwargs):
            ids = list(db.connection().execute(
                text('SELECT id FROM print_files WHERE stored_path LIKE :path'),
                {'path': str(importer[2]) + '/%'}).scalars())
            if len(ids) < 3:
                return flush(*args, **kwargs)
            reserved.extend(ids)
            raise OSError('private batch failure')
        def fail_rollback():
            raise OSError('private rollback failure')
        monkeypatch.setattr(db, 'flush', fail_flush)
        monkeypatch.setattr(db, 'rollback', fail_rollback)
        try:
            return original(db, *args)
        finally:
            monkeypatch.setattr(db, 'flush', flush)
            monkeypatch.setattr(db, 'rollback', rollback)
    def rescue(source, destination):
        attempts.append(source)
        assert source.exists()
        assert 'reserved file references' in caplog.text
        if failed_rescue and len(attempts) == 1:
            raise PermissionError('private rescue failure')
        return replace(source, destination)
    monkeypatch.setattr(plate_imports, '_persist', persist)
    monkeypatch.setattr(plate_imports.os, 'replace', rescue)
    response = upload(importer)
    assert response.status_code == 500, response.text
    assert 'outcome is uncertain' in response.json()['detail']
    assert len(reserved) == len(attempts) == 3
    assert all(str(file_id) in response.json()['detail'] and str(file_id) in caplog.text for file_id in reserved)
    assert 'private' not in response.text and 'private' not in caplog.text
    assert not list(importer[2].glob('.plate-import-*'))
    assert len(list(importer[2].rglob('*.gcode'))) == 3 - int(failed_rescue)
    if failed_rescue:
        assert f'retention failed for file reference {reserved[0]} (PermissionError)' in caplog.text
        assert 'staging will be removed' in caplog.text
