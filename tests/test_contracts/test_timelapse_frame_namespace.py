"""Actual durable namespace identity on disposable DB and owned filesystem."""
import importlib
import os
from pathlib import Path
import pytest
from sqlalchemy import text
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from modules.archives.timelapse_frame_namespace import bind_or_open_namespace, FrameNamespaceUnavailable
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_timelapse_content_audience import capture_plain


@pytest.fixture
def captured(review_db, tmp_path):
    tid = capture_plain(review_db, org=1)
    review_db.execute(text("UPDATE timelapses SET status='capturing' WHERE id=:id"), {'id':tid})
    review_db.execute(text("UPDATE jobs SET status='printing' WHERE id=444"))
    review_db.commit()
    migration = importlib.import_module('core.schema.migrations.019_timelapse_publications')
    migration.apply(review_db.connection()); review_db.commit()
    nonce = review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'), {'id':tid}).scalar_one()
    review_db.rollback()
    root = tmp_path / 'private-media'; root.mkdir(mode=0o700)
    return tid, nonce, root, migration


def opened(db, captured, *, nonce=None, commit=True):
    tid, expected, root, _ = captured
    with acquire_media_guard(root, tid) as guard, _root(root) as (fd, identity):
        try:
            descriptor = bind_or_open_namespace(db, fd, identity, guard, nonce=nonce or expected,
                                                timelapse_id=tid, printer_id=77, job_id=444)
        except Exception:
            db.rollback()
            guard.assert_owned()
            raise
        try:
            inode = os.fstat(descriptor).st_ino
            if commit: db.commit()
            else: db.rollback()
            guard.assert_owned()
            return inode
        finally: os.close(descriptor)


def test_committed_binding_reopens_recorded_inode_and_validates_retained_schema(review_db, captured):
    inode = opened(review_db, captured); review_db.commit()
    assert opened(review_db, captured) == inode
    captured[3].validate(review_db.connection())
    review_db.rollback()
    assert len(list(captured[2].glob('frames_*'))) == 1


@pytest.mark.parametrize('change', ['nonce', 'retired', 'job_stopped', 'unowned'])
def test_stale_capture_identity_cannot_create_namespace(review_db, captured, change):
    if change == 'retired':
        review_db.execute(text("UPDATE timelapses SET status='deleting' WHERE id=:id"), {'id':captured[0]})
    elif change == 'job_stopped': review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"))
    elif change == 'unowned': review_db.execute(text('UPDATE timelapses SET ownership_operation_id=NULL WHERE id=:id'), {'id':captured[0]})
    review_db.commit()
    with pytest.raises(FrameNamespaceUnavailable):
        opened(review_db, captured, nonce='f'*32 if change == 'nonce' else None)
    review_db.rollback()
    assert list(captured[2].glob('frames_*')) == []


def test_unbound_numeric_directory_is_not_adopted(review_db, captured):
    (captured[2] / str(captured[0])).mkdir()
    with pytest.raises(FrameNamespaceUnavailable): opened(review_db, captured)
    review_db.rollback()
    assert list(captured[2].glob('frames_*')) == []


def test_replaced_bound_directory_is_refused(review_db, captured):
    opened(review_db, captured); review_db.commit()
    directory = next(captured[2].glob('frames_*'))
    directory.rename(captured[2] / 'retained-original')
    directory.mkdir(mode=0o700)
    with pytest.raises(FrameNamespaceUnavailable): opened(review_db, captured)
    review_db.rollback()


def test_rollback_never_adopts_unbound_empty_directory(review_db, captured):
    first = opened(review_db, captured, commit=False)
    second = opened(review_db, captured); review_db.commit()
    assert second != first
    assert len(list(captured[2].glob('frames_*'))) == 2


def test_stale_nonce_cannot_follow_reused_numeric_id(review_db, captured):
    from core.timelapse_ownership_schema import anchors
    import uuid
    opened(review_db, captured); review_db.commit()
    old = dict(review_db.execute(anchors.select().where(anchors.c.operation_id == captured[1])).mappings().one())
    replacement = uuid.uuid4().hex
    old['operation_id'] = replacement
    old['filename'] = 'printer_77/' + replacement + '.mp4'
    review_db.execute(anchors.insert().values(**old))
    review_db.execute(text('UPDATE timelapses SET ownership_operation_id=:nonce,filename=:name WHERE id=:id'),
                      {'nonce': replacement, 'name': old['filename'], 'id': captured[0]})
    review_db.commit()
    with pytest.raises(FrameNamespaceUnavailable): opened(review_db, captured)
    review_db.rollback()
    assert len(list(captured[2].glob('frames_*'))) == 1


def test_symlink_replacement_never_opens_an_external_directory(review_db, captured, tmp_path):
    opened(review_db, captured); review_db.commit()
    directory = next(captured[2].glob('frames_*'))
    directory.rename(captured[2] / 'original-bound')
    external = tmp_path / 'external'; external.mkdir(mode=0o700)
    directory.symlink_to(external, target_is_directory=True)
    with pytest.raises(FrameNamespaceUnavailable): opened(review_db, captured)
    review_db.rollback()
    assert list(external.iterdir()) == []


@pytest.mark.parametrize('failed_sync', [1, 2])
def test_directory_or_parent_sync_failure_cannot_commit_binding(review_db, captured, monkeypatch, failed_sync):
    from modules.archives import timelapse_frame_namespace as service
    original = os.fsync
    calls = []
    def synced(fd):
        calls.append(fd)
        if len(calls) == failed_sync: raise OSError('Synthetic sync failure')
        return original(fd)
    monkeypatch.setattr(service.os, 'fsync', synced)
    with pytest.raises(FrameNamespaceUnavailable): opened(review_db, captured)
    review_db.rollback()
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapse_frame_bindings')).scalar_one() == 0
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapse_media_lifetimes')).scalar_one() == 0
    assert len(calls) == failed_sync
