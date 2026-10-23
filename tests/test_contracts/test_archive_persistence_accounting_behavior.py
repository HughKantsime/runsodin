"""Real isolated archive persistence; provider delivery is substituted only."""
from contextlib import contextmanager
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def archive_case(review_db,monkeypatch):
 from modules.archives import archive
 engine=review_db.get_bind()
 with engine.begin() as conn:
  conn.execute(text("INSERT INTO jobs(id,item_name,submitted_by,estimated_cost,print_file_id) VALUES(7099,'Synthetic reprint',3,2.5,7099)"))
  conn.execute(text("INSERT INTO print_files(id,filename,stored_path,job_id,filament_weight_grams,plate_count,thumbnail_b64,org_id) VALUES(7099,'Synthetic.3mf','/opaque/archive-synthetic.3mf',7000,80,2,'synthetic-thumbnail',1)"))
  conn.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at,ended_at) VALUES(7099,9,7099,'Synthetic print','completed','2026-10-11 12:00:00','2026-10-11 13:00:00')"))
  conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(7099,'Synthetic','Synthetic PLA')"))
  conn.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g,org_id) VALUES(7099,7099,1000,1),(7100,7099,1000,1),(7101,7099,1000,2)"))
  conn.execute(text("UPDATE filament_slots SET assigned_spool_id=7099 WHERE printer_id=9 AND slot_number=1"))
  conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,assigned_spool_id) VALUES(9,2,'PLA',7100)"))
  # Explicit first-start evidence replaces the original legacy fixture's
  # unsupported assumption that current slots prove physical consumption.
  conn.execute(text("UPDATE jobs SET printer_id=9,charged_to_org_id=1,ams_mapping='[0,1]' WHERE id=7099"))
  conn.execute(text('UPDATE print_files SET filaments_json=:materials WHERE id=7099'),{'materials':json.dumps([{'slot':0,'used_grams':40},{'slot':1,'used_grams':40}])})
  conn.execute(text("UPDATE print_jobs SET status='running' WHERE id=7099"))
  from modules.archives.attempt_accounting import capture_attempt_spools
  assert capture_attempt_spools(conn,7099,9,source='synthetic-first-start')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
 @contextmanager
 def database():
  conn=engine.raw_connection()
  try:yield conn
  except BaseException:conn.rollback();raise
  finally:conn.close()
 monkeypatch.setattr(archive,'get_db',database)
 delivery=[];monkeypatch.setattr(archive,'_sync_spoolman_consumption',lambda *args:delivery.append(args))
 return archive,engine

def test_reused_explicit_file_retains_archive_metadata(archive_case):
 archive,engine=archive_case;archive.create_print_archive(7099,9,True)
 with engine.connect() as conn:
  row=conn.execute(text('SELECT print_file_id,file_path,filament_used_grams,plate_count,thumbnail_b64 FROM print_archives WHERE print_job_id=7099')).one()
  assert tuple(row)==(7099,'/opaque/archive-synthetic.3mf',80,2,'synthetic-thumbnail')

def test_offset_timestamps_preserve_elapsed_duration(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text("UPDATE print_jobs SET started_at='2026-10-11T12:00:00+02:00',ended_at='2026-10-11T11:30:00+01:00' WHERE id=7099"))
 archive.create_print_archive(7099,9,True)
 with engine.connect() as conn:assert conn.execute(text('SELECT actual_duration_seconds FROM print_archives WHERE print_job_id=7099')).scalar_one()==1800

def test_repeat_completion_creates_one_archive_and_one_deduction(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text('UPDATE print_files SET job_id=7099 WHERE id=7099'))
 archive.create_print_archive(7099,9,True);archive.create_print_archive(7099,9,True)
 with engine.connect() as conn:
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==1
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[960,960]

def test_foreign_slot_never_deducts_foreign_inventory(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text('UPDATE print_files SET job_id=7099 WHERE id=7099'));conn.execute(text('UPDATE filament_slots SET assigned_spool_id=7101 WHERE printer_id=9 AND slot_number=2'))
 archive.create_print_archive(7099,9,True)
 with engine.connect() as conn:assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7101')).scalar_one()==1000

def test_same_spool_in_two_slots_deducts_total_once(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  # Recreate pre-036 history; migration must retain these ambiguous links.
  conn.execute(text('DROP INDEX uq_filament_slots_spool'))
  conn.execute(text('UPDATE print_files SET job_id=7099 WHERE id=7099'));conn.execute(text('UPDATE filament_slots SET assigned_spool_id=7099 WHERE printer_id=9 AND slot_number=2'))
  import importlib
  migration = importlib.import_module('core.schema.migrations.036_ctec_pilot')
  migration.apply(conn)
  assert not migration.retry_spool_index(conn)
  conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
  from modules.archives.attempt_accounting import capture_attempt_spools
  assert capture_attempt_spools(conn,7099,9,source='synthetic-same-spool-first-start')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
 archive.create_print_archive(7099,9,True)
 with engine.connect() as conn:assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==920

def test_consumption_write_failure_rolls_back_archive_and_spools(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text('UPDATE print_files SET job_id=7099 WHERE id=7099'))
  conn.execute(text("CREATE TRIGGER fail_archive_consumption BEFORE UPDATE OF consumption_json ON print_archives BEGIN SELECT RAISE(ABORT,'synthetic persistence failure'); END"))
 archive.create_print_archive(7099,9,True)
 with engine.connect() as conn:
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==0
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[1000,1000]

def test_legacy_missing_capture_is_archived_as_unresolved_without_guess(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text('UPDATE print_jobs SET spool_snapshot=NULL WHERE id=7099'))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT accounting_state,accounting_reason,consumption_json FROM print_archives WHERE print_job_id=7099')).one())==('unresolved','missing_or_invalid_start_snapshot',None)
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[1000,1000]
  assert conn.execute(text('SELECT COUNT(*) FROM spool_usage WHERE print_job_id=7099')).scalar_one()==0

def test_two_physical_attempts_of_same_job_account_separately(archive_case):
 archive,engine=archive_case
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.begin() as conn:
  conn.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at,ended_at) VALUES(7102,9,7099,'Synthetic reprint','running','2026-10-11 14:00:00','2026-10-11 15:00:00')"))
  from modules.archives.attempt_accounting import capture_attempt_spools
  assert capture_attempt_spools(conn,7102,9,source='synthetic-second-start')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7102"))
 assert archive.create_print_archive(7102,9,True) is not None
 with engine.connect() as conn:
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE job_id=7099')).scalar_one()==2
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[920,920]
  assert conn.execute(text('SELECT DISTINCT print_job_id FROM spool_usage WHERE job_id=7099 ORDER BY print_job_id')).scalars().all()==[7099,7102]

def test_changed_spool_owner_leaves_all_balances_unchanged(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text('UPDATE spools SET org_id=2 WHERE id=7100'))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT accounting_state,accounting_reason FROM print_archives WHERE print_job_id=7099')).one())==('unresolved','spool_owner_changed')
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[1000,1000]
  assert conn.execute(text('SELECT COUNT(*) FROM spool_usage WHERE print_job_id=7099')).scalar_one()==0

@pytest.mark.parametrize('definition',[
 "BEFORE INSERT ON print_archives",
 "BEFORE UPDATE OF remaining_weight_g ON spools",
 "BEFORE INSERT ON spool_usage",
 "BEFORE INSERT ON filament_delivery_outbox",
 "BEFORE UPDATE OF accounting_state ON print_archives",
])
def test_each_persistence_failure_rolls_back_entire_archive_transaction(archive_case,definition):
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text('UPDATE spools SET spoolman_spool_id=123 WHERE id=7099'))
  conn.execute(text('CREATE TRIGGER synthetic_atomic_failure '+definition+" BEGIN SELECT RAISE(ABORT,'synthetic transaction failure'); END"))
 assert archive.create_print_archive(7099,9,True) is None
 with engine.connect() as conn:
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==0
  assert conn.execute(text('SELECT COUNT(*) FROM spool_usage WHERE print_job_id=7099')).scalar_one()==0
  assert conn.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox WHERE print_job_id=7099')).scalar_one()==0
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[1000,1000]

def test_provider_delivery_is_queued_once_without_request_in_local_transaction(archive_case,monkeypatch):
 archive,engine=archive_case
 def forbidden(*args):raise AssertionError('provider delivery occurred inside local archive transaction')
 monkeypatch.setattr(archive,'_sync_spoolman_consumption',forbidden)
 with engine.begin() as conn:conn.execute(text('UPDATE spools SET spoolman_spool_id=123 WHERE id=7099'))
 assert archive.create_print_archive(7099,9,True) is not None
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT print_job_id,spool_id,spoolman_spool_id,grams_used,state FROM filament_delivery_outbox')).one())==(7099,7099,123,40,'pending')
  consumed=json.loads(conn.execute(text('SELECT consumption_json FROM print_archives WHERE print_job_id=7099')).scalar_one())
  assert all(x['quantity_provenance']=='sliced_estimate' for x in consumed)

@pytest.mark.parametrize('grams,reason',[(96,'measured_material_distribution_unknown'),(-1,'invalid_measured_quantity')])
def test_total_measurement_cannot_guess_multicolor_distribution(archive_case,grams,reason):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text('UPDATE print_jobs SET filament_used_g=:grams WHERE id=7099'),{'grams':grams})
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT accounting_state,accounting_reason FROM print_archives WHERE print_job_id=7099')).one())==('unresolved',reason)
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[1000,1000]

def test_legacy_scheduled_usage_is_not_assumed_to_identify_current_attempt(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text('INSERT INTO spool_usage(spool_id,job_id,weight_used_g) VALUES(7099,7099,40)'))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert conn.execute(text('SELECT accounting_reason FROM print_archives WHERE print_job_id=7099')).scalar_one()=='legacy_usage_unattributed'
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==1000

def test_wrong_printer_cannot_create_archive_or_account_foreign_attempt(archive_case):
 archive,engine=archive_case
 assert archive.create_print_archive(7099,10,True) is None
 with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==0

def test_premature_archive_request_does_not_steal_real_completion(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text("UPDATE print_jobs SET status='running',ended_at=NULL WHERE id=7099"))
 assert archive.create_print_archive(7099,9,True,result_status='completed') is None
 with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==0
 with engine.begin() as conn:conn.execute(text("UPDATE print_jobs SET status='completed',ended_at='2026-10-11 13:00:00' WHERE id=7099"))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==960

def test_caller_status_override_cannot_rewrite_authoritative_outcome(archive_case):
 archive,engine=archive_case
 assert archive.create_print_archive(7099,9,False,result_status='cancelled') is None
 with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==0

def test_inventory_shortfall_preserves_quantity_and_exposes_discrepancy(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text('UPDATE spools SET remaining_weight_g=10 WHERE id=7099'))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  state,reason,raw=conn.execute(text('SELECT accounting_state,accounting_reason,consumption_json FROM print_archives WHERE print_job_id=7099')).one()
  assert (state,reason)==('reconciliation_required','inventory_shortfall')
  assert next(x for x in json.loads(raw) if x['spool_id']==7099)['inventory_shortfall_g']==30
  assert conn.execute(text('SELECT weight_used_g FROM spool_usage WHERE print_job_id=7099 AND spool_id=7099')).scalar_one()==40
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==0

def test_failed_persistence_remains_recoverable_on_next_registration(archive_case):
 from types import SimpleNamespace
 archive,engine=archive_case
 with engine.begin() as conn:conn.execute(text("CREATE TRIGGER synthetic_recovery_failure BEFORE INSERT ON spool_usage BEGIN SELECT RAISE(ABORT,'synthetic retryable failure'); END"))
 assert archive.recover_missing_archives()=={'attempted':1,'recovered':0,'pending_count':1,'next_after_id':7099}
 with engine.connect() as conn:
  assert conn.execute(text('SELECT status FROM print_jobs WHERE id=7099')).scalar_one()=='completed'
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==0
 with engine.begin() as conn:conn.execute(text('DROP TRIGGER synthetic_recovery_failure'))
 subscriptions=[]
 archive.register_subscribers(SimpleNamespace(subscribe=lambda *args:subscriptions.append(args)))
 assert len(subscriptions)==3
 assert archive.recover_missing_archives()=={'attempted':0,'recovered':0,'pending_count':0,'next_after_id':None}
 with engine.connect() as conn:
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==960
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==1

def test_corrupted_plan_cannot_substitute_other_captured_slot_inventory(archive_case):
 archive,engine=archive_case
 with engine.begin() as conn:
  raw=conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7099')).scalar_one()
  snapshot=json.loads(raw)
  snapshot['material_plan']['allocations'][0]['spool_id']=7100
  conn.execute(text('UPDATE print_jobs SET spool_snapshot=:snapshot WHERE id=7099'),{'snapshot':json.dumps(snapshot)})
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  assert conn.execute(text('SELECT accounting_reason FROM print_archives WHERE print_job_id=7099')).scalar_one()=='contradictory_start_identity'
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()==[1000,1000]

def test_stale_success_flag_does_not_prevent_authoritative_completed_accounting(archive_case):
 archive,engine=archive_case
 assert archive.create_print_archive(7099,9,False) is not None
 with engine.connect() as conn:
  assert conn.execute(text('SELECT accounting_state FROM print_archives WHERE print_job_id=7099')).scalar_one()=='accounted'
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==960

@pytest.mark.parametrize('status',['failed','cancelled'])
def test_partial_unsuccessful_single_material_measured_consumption_is_accounted(archive_case,status):
 archive,engine=archive_case
 from modules.archives.attempt_accounting import capture_attempt_spools
 with engine.begin() as conn:
  conn.execute(text("UPDATE jobs SET ams_mapping='[0]' WHERE id=7099"))
  conn.execute(text('UPDATE print_files SET filaments_json=:materials WHERE id=7099'),{'materials':json.dumps([{'slot':0,'used_grams':80}])})
  conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
  assert capture_attempt_spools(conn,7099,9,source='synthetic-partial-start')
  conn.execute(text('UPDATE print_jobs SET status=:status,filament_used_g=12 WHERE id=7099'),{'status':status})
 assert archive.create_print_archive(7099,9,False,result_status=status) is not None
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT status,filament_used_grams,accounting_state FROM print_archives WHERE print_job_id=7099')).one())==(status,12,'accounted')
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==988
  assert conn.execute(text('SELECT weight_used_g FROM spool_usage WHERE print_job_id=7099')).scalar_one()==12

def test_recovery_cursor_advances_past_persistent_failure(archive_case):
 archive,engine=archive_case
 from modules.archives.attempt_accounting import capture_attempt_spools
 with engine.begin() as conn:
  conn.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at,ended_at) VALUES(7102,9,7099,'Synthetic second','running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
  assert capture_attempt_spools(conn,7102,9,source='synthetic-second-start')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7102"))
  conn.execute(text("CREATE TRIGGER synthetic_first_recovery_failure BEFORE INSERT ON print_archives WHEN NEW.print_job_id=7099 BEGIN SELECT RAISE(ABORT,'synthetic persistent failure'); END"))
 first=archive.recover_missing_archives(limit=1)
 assert first=={'attempted':1,'recovered':0,'pending_count':2,'next_after_id':7099}
 second=archive.recover_missing_archives(limit=1,after_attempt_id=first['next_after_id'])
 assert second=={'attempted':1,'recovered':1,'pending_count':1,'next_after_id':7102}
 with engine.connect() as conn:
  assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one()==960
  assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7102')).scalar_one()==1
