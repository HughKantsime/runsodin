"""Immutable archive content ownership through real first-start/persistence."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case
from tests.test_contracts.test_education_review_workflow import review_db


def test_new_archive_has_first_start_private_owner(archive_case):
 archive,engine=archive_case
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  row=conn.execute(text('SELECT org_id,ownership_state,ownership_source FROM print_archives WHERE print_job_id=7099')).one()
  assert tuple(row)==(1,'known','first_start')


def test_printer_reassignment_does_not_change_captured_archive_owner(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
  conn.execute(text('UPDATE printers SET org_id=2 WHERE id=9'))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT org_id,ownership_state FROM print_archives WHERE print_job_id=7099')).one())==(1,'known')


def test_changed_source_graph_is_unresolved_and_excludes_new_tenant_metadata(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=7099'))
  conn.execute(text("UPDATE print_files SET org_id=2,thumbnail_b64='new-tenant-private' WHERE id=7099"))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  row=conn.execute(text('SELECT org_id,ownership_state,thumbnail_b64,file_path,print_file_id FROM print_archives WHERE print_job_id=7099')).one()
  assert tuple(row)==(1,'unresolved',None,None,None)


def test_unattributed_manual_attempt_does_not_inherit_printer_owner(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL,spool_snapshot=NULL WHERE id=7099'))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT org_id,ownership_state FROM print_archives WHERE print_job_id=7099')).one())==(None,'unresolved')


def test_first_start_retains_valid_education_marker(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
  conn.execute(text('UPDATE jobs SET print_file_id=11,printer_id=9 WHERE id=13'))
  conn.execute(text("UPDATE print_jobs SET scheduled_job_id=13,spool_snapshot=NULL,status='running' WHERE id=7099"))
  from modules.archives.attempt_accounting import capture_attempt_spools
  assert capture_attempt_spools(conn,7099,9,source='synthetic-edu-start')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  row=conn.execute(text('SELECT org_id,ownership_state,ownership_source,education_submission_id FROM print_archives WHERE print_job_id=7099')).one()
  assert tuple(row)==(1,'known','first_start',14)
