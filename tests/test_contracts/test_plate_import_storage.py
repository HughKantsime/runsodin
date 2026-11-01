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
