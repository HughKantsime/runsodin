"""Actual daemon creation must persist ownership before any camera capture."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db

@pytest.fixture
def owner_capture(review_db,tmp_path,monkeypatch):
    from modules.archives import timelapse_capture
    monkeypatch.setattr(timelapse_capture,'TIMELAPSE_DIR',tmp_path)
    review_db.execute(text("UPDATE jobs SET printer_id=9,status='printing' WHERE id=13"))
    review_db.execute(text("UPDATE printers SET shared=0 WHERE id=9"));review_db.commit()
    return timelapse_capture


def test_first_education_capture_retains_immutable_owner_and_submission(review_db,owner_capture):
    # Complete the existing owned Education source graph, without a provider call.
    review_db.execute(text('UPDATE jobs SET print_file_id=11 WHERE id=13'))
    review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"));review_db.commit()
    from modules.archives.ownership import capture_content_owner
    source=capture_content_owner(review_db.connection(),13)
    assert (source['state'],source['org_id'],source['education_submission_id'])==('known',1,14)
    tid=owner_capture.get_or_create_timelapse(review_db,9,13)
    row=review_db.execute(text('SELECT ownership_operation_id,filename FROM timelapses WHERE id=:id'),{'id':tid}).one()
    assert row.ownership_operation_id is not None
    captured=review_db.execute(text('SELECT timelapse_id,printer_id,scheduled_job_id,filename,owner_state,owner_org_id,print_file_id,education_submission_id,capture_printer_org_id,capture_printer_shared FROM timelapse_ownership_anchors WHERE operation_id=:nonce'),{'nonce':row.ownership_operation_id}).one()
    assert tuple(captured)==(tid,9,13,row.filename,'known',1,11,14,1,0)
    assert owner_capture.get_or_create_timelapse(review_db,9,13)==tid
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==1


def test_existing_legacy_capture_is_not_assigned_owner_from_current_printer(review_db,owner_capture):
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(199,9,13,'legacy.mp4','capturing')"));review_db.commit()
    assert owner_capture.get_or_create_timelapse(review_db,9,13)==199
    assert review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=199')).scalar_one() is None
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==0


@pytest.mark.parametrize('shared,expected',[(False,'unresolved'),(True,'shared')])
def test_global_content_needs_explicit_capture_time_camera_sharing(review_db,owner_capture,shared,expected):
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'global.3mf','global.3mf','/owned/global.3mf',NULL)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,status) VALUES(444,'Global print',9,111,'printing')"))
    review_db.execute(text('UPDATE printers SET shared=:shared WHERE id=9'),{'shared':shared});review_db.commit()
    from modules.archives.ownership import capture_content_owner
    source=capture_content_owner(review_db.connection(),444)
    assert (source['state'],source['org_id'],source['education_submission_id'])==('known',None,None)
    tid=owner_capture.get_or_create_timelapse(review_db,9,444)
    captured=review_db.execute(text('SELECT a.owner_state,a.owner_org_id,a.capture_printer_org_id,a.capture_printer_shared FROM timelapses t JOIN timelapse_ownership_anchors a ON a.operation_id=t.ownership_operation_id WHERE t.id=:id'),{'id':tid}).first()
    assert captured is not None
    assert tuple(captured)==(expected,None,1,int(shared))


@pytest.mark.parametrize('fault',['anchor_write','commit','acknowledgment'])
def test_owner_and_capture_commit_together_and_uncertain_retry_is_exact(review_db,owner_capture,fault):
    from sqlalchemy.orm import Session
    from sqlalchemy import event
    engine=review_db.get_bind()
    class OwnedFault(Session):
        def commit(self):
            if fault=='commit':raise RuntimeError('owned commit refused')
            super().commit()
            if fault=='acknowledgment':raise RuntimeError('owned commit acknowledgment uncertainty')
    def refuse(_connection,_cursor,statement,_parameters,_context,_many):
        if fault=='anchor_write' and statement.lstrip().startswith('INSERT INTO timelapse_ownership_anchors'):
            raise RuntimeError('owned anchor write refused')
    event.listen(engine,'before_cursor_execute',refuse)
    try:
        with OwnedFault(engine) as db:
            with pytest.raises(RuntimeError,match='Timelapse creation could not be confirmed'):
                owner_capture.get_or_create_timelapse(db,9,13)
    finally:event.remove(engine,'before_cursor_execute',refuse)
    with Session(engine) as db:
        count=db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()
        anchors=db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()
        assert count==anchors==(1 if fault=='acknowledgment' else 0)
        db.rollback()
        if fault=='acknowledgment':
            tid=owner_capture.get_or_create_timelapse(db,9,13)
            assert db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()==1
            assert db.execute(text('SELECT timelapse_id FROM timelapse_ownership_anchors')).scalar_one()==tid


def test_existing_capture_owner_does_not_follow_reassigned_printer(review_db,owner_capture):
    # A separate non-Education printer permits actual reassignment without
    # weakening the existing Education printer/cost-center integrity guards.
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(77,'Owned capture printer',1,0)"))
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'owned.3mf','owned.3mf','/owned/plain.3mf',1)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,charged_to_org_id,status) VALUES(444,'Owned print',77,111,1,'printing')"));review_db.commit()
    tid=owner_capture.get_or_create_timelapse(review_db,77,444)
    review_db.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=77'));review_db.commit()
    assert owner_capture.get_or_create_timelapse(review_db,77,444)==tid
    row=review_db.execute(text('SELECT a.owner_org_id,a.capture_printer_org_id,a.capture_printer_shared FROM timelapses t JOIN timelapse_ownership_anchors a ON a.operation_id=t.ownership_operation_id WHERE t.id=:tid'),{'tid':tid}).one()
    assert tuple(row)==(1,1,0)


@pytest.mark.parametrize('stored',[2,-1,'false',None])
def test_stored_malformed_sharing_never_grants_global_media(review_db,owner_capture,stored):
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'global.3mf','global.3mf','/owned/global.3mf',NULL)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,status) VALUES(444,'Global print',9,111,'printing')"))
    review_db.execute(text('UPDATE printers SET shared=:value WHERE id=9'),{'value':stored});review_db.commit()
    with pytest.raises(RuntimeError,match='Timelapse creation could not be confirmed'):
        owner_capture.get_or_create_timelapse(review_db,9,444)
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()==0
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==0


@pytest.mark.parametrize('field,value',[('printer_id',10),('status','completed')])
def test_independently_committed_job_drift_refuses_stale_capture_selection(review_db,owner_capture,field,value):
    from sqlalchemy.orm import Session
    engine=review_db.get_bind()
    with Session(engine) as stale:
        assert stale.execute(text('SELECT printer_id,status FROM jobs WHERE id=13')).one()==(9,'printing')
        with Session(engine) as writer:
            if field=='printer_id':writer.execute(text('UPDATE jobs SET printer_id=:value WHERE id=13'),{'value':value})
            else:writer.execute(text('UPDATE jobs SET status=:value WHERE id=13'),{'value':value})
            writer.commit()
        with pytest.raises(RuntimeError,match='Timelapse creation could not be confirmed'):
            owner_capture.get_or_create_timelapse(stale,9,13)
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapses')).scalar_one()==0
    assert review_db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==0
