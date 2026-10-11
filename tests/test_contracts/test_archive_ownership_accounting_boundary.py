"""Unresolved content authority must not authorize inventory mutation/delivery."""
from sqlalchemy import text
from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case
from tests.test_contracts.test_education_review_workflow import review_db


def test_operation_owned_unresolved_archive_cannot_deduct_or_queue_provider_usage(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text("UPDATE education_upload_operations SET final_path='/opaque/archive-synthetic.3mf' WHERE operation_id='upload-1'"))
  conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
  from modules.archives.attempt_accounting import capture_attempt_spools
  assert capture_attempt_spools(conn,7099,9,source='synthetic-unresolved-owner')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT ownership_state,accounting_state,accounting_reason FROM print_archives WHERE print_job_id=7099')).one())==('unresolved','unresolved','archive_ownership_unresolved')
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN(7099,7100) ORDER BY id')).scalars().all()==[1000,1000]
  assert conn.execute(text('SELECT COUNT(*) FROM spool_usage WHERE print_job_id=7099')).scalar_one()==0
  assert conn.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox WHERE print_job_id=7099')).scalar_one()==0
