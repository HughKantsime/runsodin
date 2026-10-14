"""Actual encoder state boundary with real frame receipts and explicit codec seam."""
import pytest
from sqlalchemy import text,select
from sqlalchemy.orm import Session
from core.timelapse_publication_schema import publications
from tests.test_contracts.test_timelapse_encoder_publication import encoder,encode,frame_writer,review_db,owned_runtime


@pytest.mark.parametrize('status',['ready','deleting','failed','encoding','unknown'])
def test_stale_encoder_does_not_republish_completed_or_retiring_media(encoder,review_db,status):
    writer,tid,root,nonce,module,orders=encoder
    filename=review_db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    original=root/filename;original.parent.mkdir();original.write_bytes(b'owned original')
    review_db.execute(text('UPDATE timelapses SET status=:status WHERE id=:id'),{'status':status,'id':tid});review_db.commit()
    assert not encode(encoder) and orders==[] and original.read_bytes()==b'owned original'
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==status


def test_capturing_encoder_still_publishes_owned_video(encoder,review_db):
    writer,tid,root,nonce,module,orders=encoder
    filename=review_db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one();review_db.rollback()
    original=root/filename;original.parent.mkdir();original.write_bytes(b'owned original')
    assert encode(encoder) and orders==[[1,3,10]]
    result=review_db.execute(text('SELECT status,filename FROM timelapses WHERE id=:id'),{'id':tid}).one()
    assert result.status=='ready' and result.filename!=filename
    assert (root/result.filename).read_bytes()==b'explicit synthetic MP4 phase evidence'
    assert original.read_bytes()==b'owned original'


def test_nonce_refuses_capture_before_any_media_work(encoder,review_db):
    writer,tid,root,nonce,module,orders=encoder
    review_db.execute(text("UPDATE timelapses SET retirement_operation_id='owned-retirement' WHERE id=:id"),{'id':tid});review_db.commit()
    assert not encode(encoder) and not orders and len(list(root.glob('frames_*/*.jpg')))==3
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='capturing'


def test_claim_commit_fault_never_starts_encoder_or_creates_output(encoder,review_db,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder
    class FailedCommit(Session):
        def commit(self):raise RuntimeError('owned injected commit failure')
    monkeypatch.setattr(writer,'SessionLocal',lambda:FailedCommit(review_db.get_bind()))
    assert not encode(encoder) and not orders and not list(root.glob('encode_*.mp4'))
    assert len(list(root.glob('frames_*/*.jpg')))==3
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='capturing'


@pytest.mark.parametrize('returncode',[0,1])
def test_state_drift_does_not_restore_ready_or_failed(encoder,review_db,monkeypatch,returncode):
    writer,tid,root,nonce,module,orders=encoder;codec=module.complete_media_transport
    def changed(*args,**kwargs):
        with writer.SessionLocal() as db:
            db.execute(text("UPDATE timelapses SET status='deleting',retirement_operation_id='owned-retirement' WHERE id=:id"),{'id':tid});db.commit()
        if returncode:raise ValueError('Owned codec failure')
        return codec(*args,**kwargs)
    monkeypatch.setattr(module,'complete_media_transport',changed)
    assert not encode(encoder) and len(list(root.glob('frames_*/*.jpg')))==3
    assert review_db.execute(text('SELECT status,retirement_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).one()==('deleting','owned-retirement')


@pytest.mark.parametrize('changed_field',['filename','printer_id'])
@pytest.mark.parametrize('exception',[False,True])
def test_identity_drift_preserves_new_record_before_ready_or_exception(encoder,review_db,monkeypatch,changed_field,exception):
    writer,tid,root,nonce,module,orders=encoder;codec=module.complete_media_transport
    changed_value='printer_77/other.mp4' if changed_field=='filename' else 9
    def changed(*args,**kwargs):
        with writer.SessionLocal() as db:
            if changed_field=='filename':db.execute(text('UPDATE timelapses SET filename=:value WHERE id=:id'),{'value':changed_value,'id':tid})
            else:db.execute(text('UPDATE timelapses SET printer_id=:value WHERE id=:id'),{'value':changed_value,'id':tid})
            db.commit()
        if exception:raise RuntimeError('Owned encoder failure')
        return codec(*args,**kwargs)
    monkeypatch.setattr(module,'complete_media_transport',changed)
    assert not encode(encoder) and len(list(root.glob('frames_*/*.jpg')))==3
    row=review_db.execute(text('SELECT status,filename,printer_id FROM timelapses WHERE id=:id'),{'id':tid}).one()
    assert row.status=='encoding' and (row.filename if changed_field=='filename' else row.printer_id)==changed_value


def test_claim_commit_acknowledgment_uncertainty_reconciles_only_exact_journal(encoder,review_db,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder
    class UncertainCommit(Session):
        def commit(self):
            super().commit();raise RuntimeError('Owned acknowledgment uncertainty after persisted commit')
    monkeypatch.setattr(writer,'SessionLocal',lambda:UncertainCommit(review_db.get_bind()))
    assert encode(encoder) and orders==[[1,3,10]]
    assert len(list(root.glob('frames_*/*.jpg')))==3
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='ready'
    assert len(review_db.execute(select(publications.c.operation_id).where(publications.c.kind=='encode')).all())==1
