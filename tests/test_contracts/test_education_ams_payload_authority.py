"""Uploaded authoritative graph, conflicting bindings and staging-time drift."""
import io,uuid
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_submission_upload import submission_db,_sliced_3mf,_principal,_ample_disk
from tests.test_contracts.test_education_review_workflow import _principal as teacher
from modules.organizations.education_ams_payload_authority import capture_payload_graph,lock_and_recheck_payload_graph,EducationPayloadUnavailable

@pytest.fixture
def uploaded(submission_db,monkeypatch):
    from modules.organizations.education_submission_service import process_sliced_3mf_submission
    db,root=submission_db;_ample_disk(monkeypatch)
    result=process_sliced_3mf_submission(db,source=io.BytesIO(_sliced_3mf()),original_filename='pilot.3mf',
        operation_id=str(uuid.uuid4()),principal=_principal(),cost_center_id=7,storage_root=root)
    return db,result


def test_capture_and_locked_recheck_match_uploaded_job(uploaded):
    db,result=uploaded
    captured=capture_payload_graph(db,submission_id=result['id'],revision=1,principal=teacher(2))
    assert captured.job_id==result['job_id'] and captured.file_id==result['print_file_id']
    db.rollback()
    assert lock_and_recheck_payload_graph(db,captured=captured,principal=teacher(2))==captured
    db.rollback()


@pytest.mark.parametrize('mutation',["UPDATE print_files SET file_hash=printf('%064d',0)",
    "UPDATE education_upload_operations SET expected_bytes=expected_bytes+1",
    "UPDATE jobs SET payload_sha256=printf('%064d',0)",
    "UPDATE print_files SET stored_path='/changed/path'",
    "UPDATE education_submissions SET lifecycle_revision=2"])
def test_graph_drift_after_staging_refuses(uploaded,mutation):
    db,result=uploaded
    captured=capture_payload_graph(db,submission_id=result['id'],revision=1,principal=teacher(2))
    db.rollback()
    db.execute(text(mutation));db.commit()
    with pytest.raises(EducationPayloadUnavailable):lock_and_recheck_payload_graph(db,captured=captured,principal=teacher(2))
    db.rollback()


def test_independent_operation_path_change_before_lock_refuses(uploaded):
    from sqlalchemy.orm import Session
    db,result=uploaded
    captured=capture_payload_graph(db,submission_id=result['id'],revision=1,principal=teacher(2))
    db.rollback()
    with Session(db.get_bind()) as other:
        other.execute(text("UPDATE education_upload_operations SET final_path='/changed-operation/path'"))
        other.commit()
    with pytest.raises(EducationPayloadUnavailable):
        lock_and_recheck_payload_graph(db,captured=captured,principal=teacher(2))
    db.rollback()
