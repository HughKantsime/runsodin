"""Actual frame writer acceptance; camera transport replaced by synthetic bytes."""
from types import SimpleNamespace
from contextlib import contextmanager
import importlib
import cv2
import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_content_audience import capture_plain


@pytest.fixture
def frame_writer(review_db,tmp_path,monkeypatch):
    from modules.archives import timelapse_capture
    tid=capture_plain(review_db,org=1)
    review_db.execute(text("UPDATE timelapses SET status='capturing' WHERE id=:id"),{'id':tid})
    review_db.execute(text("UPDATE jobs SET status='printing' WHERE id=444"))
    review_db.execute(text('UPDATE printers SET timelapse_enabled=1,is_active=1 WHERE id=77'));review_db.commit()
    importlib.import_module('core.schema.migrations.019_timelapse_publications').apply(review_db.connection());review_db.commit()
    nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    review_db.rollback()
    root=tmp_path/'owned-media';root.mkdir(mode=0o700)
    monkeypatch.setattr(timelapse_capture,'TIMELAPSE_DIR',root)
    monkeypatch.setattr(timelapse_capture,'SessionLocal',lambda:Session(review_db.get_bind()))
    valid,data=cv2.imencode('.jpg',np.full((40,40,3),100,dtype=np.uint8));assert valid
    @contextmanager
    def stream(*args,**kwargs):yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[data.tobytes()])
    monkeypatch.setattr(timelapse_capture.httpx,'stream',stream)
    return timelapse_capture,tid,root,nonce


@pytest.mark.parametrize('state',['missing','ready','failed','deleting','encoding','unknown_owner','changed_filename','wrong_printer'])
def test_stale_or_unowned_frame_request_never_recreates_media(review_db,frame_writer,state):
    writer,tid,root,nonce=frame_writer
    if state=='missing':review_db.execute(text('DELETE FROM timelapses WHERE id=:id'),{'id':tid})
    elif state=='unknown_owner':review_db.execute(text('UPDATE timelapses SET ownership_operation_id=NULL WHERE id=:id'),{'id':tid})
    elif state=='changed_filename':review_db.execute(text("UPDATE timelapses SET filename='unrelated.mp4' WHERE id=:id"),{'id':tid})
    elif state!='wrong_printer':review_db.execute(text('UPDATE timelapses SET status=:state WHERE id=:id'),{'id':tid,'state':state})
    review_db.commit()
    assert writer.capture_frame(9 if state=='wrong_printer' else 77,tid,nonce=nonce,job_id=444) is False
    assert not list(root.glob('frames_*/*.jpg'))


def test_state_changed_during_camera_fetch_is_rechecked_before_write(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer
    @contextmanager
    def camera(*args,**kwargs):
        with Session(review_db.get_bind()) as db:
            db.execute(text("UPDATE timelapses SET status='deleting' WHERE id=:id"),{'id':tid});db.commit()
        valid,data=cv2.imencode('.jpg',np.full((40,40,3),100,dtype=np.uint8));assert valid
        yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[data.tobytes()])
    monkeypatch.setattr(writer.httpx,'stream',camera)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is False
    assert not list(root.glob('frames_*/*.jpg'))


def test_existing_frame_is_not_overwritten_when_numbering_has_a_gap(frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is True
    from modules.archives import timelapse_frame_publication as journal
    original_write=journal.os.write
    def refuse(*args):raise OSError('Reserve a real blocked frame number')
    monkeypatch.setattr(journal.os,'write',refuse)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is False
    monkeypatch.setattr(journal.os,'write',original_write)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is True
    frames=next(root.glob('frames_*'))
    existing=frames/'frame_000003.jpg';original=existing.read_bytes()
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is True
    assert existing.read_bytes()==original
    assert len(list(frames.glob('*.jpg')))==3


def test_symlink_capture_parent_never_writes_outside_owned_media(frame_writer,tmp_path):
    writer,tid,root,nonce=frame_writer;outside=tmp_path/'owned-outside';outside.mkdir()
    (root/str(tid)).symlink_to(outside,target_is_directory=True)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is False
    assert list(outside.iterdir())==[]


@pytest.mark.parametrize('change', ['retired', 'unowned'])
def test_camera_transport_is_not_entered_without_current_capture_authority(review_db, frame_writer, monkeypatch, change):
    writer, tid, root, nonce = frame_writer
    if change == 'retired':
        review_db.execute(text("UPDATE timelapses SET status='deleting' WHERE id=:id"), {'id': tid})
    else:
        review_db.execute(text('UPDATE timelapses SET ownership_operation_id=NULL WHERE id=:id'), {'id': tid})
    review_db.commit()
    calls = []
    @contextmanager
    def camera(*args, **kwargs):
        calls.append(True)
        yield SimpleNamespace(status_code=200, headers={}, iter_raw=lambda **kw:[])
    monkeypatch.setattr(writer.httpx, 'stream', camera)
    result = writer.capture_frame(77, tid, nonce=nonce, job_id=444)
    assert calls == []
    assert result is False
    assert not (root / str(tid)).exists()


def test_expected_nonce_and_job_are_required_before_camera(frame_writer, monkeypatch):
    writer, tid, root, nonce = frame_writer
    calls = []
    @contextmanager
    def camera(*args, **kwargs):
        calls.append(True)
        yield SimpleNamespace(status_code=200, headers={}, iter_raw=lambda **kw: [])
    monkeypatch.setattr(writer.httpx, 'stream', camera)
    for expected, job in [(None, 444), ('0' * 32, 444), (nonce, None), (nonce, 445)]:
        assert writer.capture_frame(77, tid, nonce=expected, job_id=job) is False
    assert calls == []
    assert not list(root.glob('frames_*/*.jpg'))


def test_restart_reopens_binding_and_updates_count_once(review_db, frame_writer):
    writer, tid, root, nonce = frame_writer
    assert writer.capture_frame(77, tid, nonce=nonce, job_id=444)
    directory = next(root.glob('frames_*'))
    identity = directory.stat().st_ino
    # A fresh call creates fresh DB sessions, rather than carrying an open namespace.
    assert writer.capture_frame(77, tid, nonce=nonce, job_id=444)
    assert directory.stat().st_ino == identity
    assert sorted(path.name for path in directory.glob('*.jpg')) == ['frame_000001.jpg', 'frame_000002.jpg']
    assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'), {'id': tid}).scalar_one() == 2
    review_db.rollback()


def test_partial_write_failure_never_publishes_frame(review_db, frame_writer, monkeypatch):
    import os
    writer, tid, root, nonce = frame_writer
    from modules.archives import timelapse_frame_capture
    original = os.write
    calls = []
    def broken(fd, payload):
        if calls:
            raise OSError('synthetic partial write failure')
        calls.append(True)
        return original(fd, payload[:5])
    monkeypatch.setattr(timelapse_frame_capture.os, 'write', broken)
    assert writer.capture_frame(77, tid, nonce=nonce, job_id=444) is False
    assert not list(root.glob('frames_*/*.jpg'))
    assert not list(root.glob('frames_*/.frame_*'))
    assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'), {'id': tid}).scalar_one() == 0
    review_db.rollback()


@pytest.mark.parametrize('payload', [b'not a JPEG', b'\xff\xd8\xff\xd9', b'\xff\xd8' + b'0' * (16 * 1024 * 1024)])
def test_invalid_or_oversized_camera_bytes_never_publish(frame_writer, monkeypatch, payload):
    writer, tid, root, nonce = frame_writer
    @contextmanager
    def camera(*args, **kwargs):
        yield SimpleNamespace(status_code=200, headers={}, iter_raw=lambda **kw: [payload])
    monkeypatch.setattr(writer.httpx, 'stream', camera)
    assert writer.capture_frame(77, tid, nonce=nonce, job_id=444) is False
    assert not list(root.glob('frames_*/*.jpg'))
