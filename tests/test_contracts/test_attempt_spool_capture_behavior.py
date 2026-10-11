"""Durable start attribution against real isolated schema, no printer transport."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def capture_case(review_db):
 engine=review_db.get_bind()
 with engine.begin() as conn:
  conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(7299,'Synthetic','PLA')"))
  conn.execute(text('INSERT INTO spools(id,filament_id,remaining_weight_g,org_id) VALUES(7299,7299,1000,1),(7300,7299,1000,2)'))
  conn.execute(text('UPDATE filament_slots SET assigned_spool_id=7299 WHERE printer_id=9 AND slot_number=1'))
  conn.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(7299,9,'Synthetic capture','running','2026-10-11 12:00:00')"))
 return engine

def capture(conn,attempt=7299,printer=9):
 from modules.archives.attempt_accounting import capture_attempt_spools
 return capture_attempt_spools(conn,attempt,printer,source='synthetic-first-start')

def snapshot(engine):
 with engine.connect() as conn:return json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7299')).scalar_one())

def test_capture_persists_start_identity_and_survives_assignment_change(capture_case):
 with capture_case.begin() as conn:assert capture(conn) is True
 original=snapshot(capture_case)
 assert original['version']==1 and original['printer_id']==9 and original['printer_org_id']==1
 assert original['source']=='synthetic-first-start'
 assert any(x['slot_number']==1 and x['spool_id']==7299 and x['spool_org_id']==1 for x in original['slots'])
 with capture_case.begin() as conn:
  conn.execute(text('UPDATE filament_slots SET assigned_spool_id=7300 WHERE printer_id=9 AND slot_number=1'))
  assert capture(conn) is False
 assert snapshot(capture_case)==original

def test_wrong_printer_does_not_capture_foreign_attempt(capture_case):
 with capture_case.begin() as conn:assert capture(conn,printer=10) is False
 with capture_case.connect() as conn:assert conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7299')).scalar_one() is None

def test_capture_is_in_callers_transaction(capture_case):
 with pytest.raises(RuntimeError):
  with capture_case.begin() as conn:
   assert capture(conn)
   raise RuntimeError('synthetic rollback')
 with capture_case.connect() as conn:assert conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7299')).scalar_one() is None

def test_foreign_and_missing_spool_identity_is_recorded_without_balance_mutation(capture_case):
 with capture_case.begin() as conn:
  conn.execute(text('UPDATE filament_slots SET assigned_spool_id=7300 WHERE printer_id=9 AND slot_number=1'))
  assert capture(conn)
 assert any(x['spool_id']==7300 and x['spool_org_id']==2 for x in snapshot(capture_case)['slots'])
 with capture_case.connect() as conn:assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7300')).scalar_one()==1000

def test_completed_attempt_never_gets_fabricated_start_snapshot(capture_case):
 with capture_case.begin() as conn:
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7299"))
  assert capture(conn) is False
 with capture_case.connect() as conn:assert conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7299')).scalar_one() is None

@pytest.mark.parametrize('path',['mqtt','generic-events','moonraker'])
def test_actual_start_paths_capture_in_same_commit(capture_case,monkeypatch,path):
 from contextlib import contextmanager
 from types import SimpleNamespace
 from core.registry import registry
 from modules.printers.monitors import mqtt_job_lifecycle,moonraker_monitor
 from modules.notifications import job_events
 @contextmanager
 def database():
  conn=capture_case.raw_connection()
  try:yield conn
  finally:conn.close()
 monkeypatch.setattr(registry,'get_provider',lambda name:None)
 monkeypatch.setattr(mqtt_job_lifecycle,'get_db',database)
 monkeypatch.setattr(job_events,'get_db',database)
 monkeypatch.setattr(job_events,'get_event_bus',lambda:SimpleNamespace(publish=lambda event:None))
 monkeypatch.setattr(moonraker_monitor,'engine',capture_case)
 with capture_case.begin() as conn:conn.execute(text('DELETE FROM print_jobs'))
 if path=='mqtt':
  identifier,_,_=mqtt_job_lifecycle.record_job_started(9,'Synthetic',{'subtask_name':'Synthetic','total_layer_num':0},lambda **kwargs:None,lambda:None)
 elif path=='generic-events':identifier=job_events.job_started(9,'Synthetic')
 else:
  instance=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor)
  instance.printer_id=9;instance.name='Synthetic';instance._current_job_db_id=None
  monkeypatch.setattr(instance,'_try_auto_link',lambda *args:None)
  instance._job_started(SimpleNamespace(filename='Synthetic',total_layers=0,bed_target=0,nozzle_target=0))
  identifier=instance._current_job_db_id
 assert identifier is not None
 with capture_case.connect() as conn:
  raw=conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()
  assert raw is not None
  assert any(x['spool_id']==7299 for x in json.loads(raw)['slots'])

def test_moonraker_resume_leaves_missing_historical_snapshot_missing(capture_case,monkeypatch):
 from types import SimpleNamespace
 from core.registry import registry
 from modules.printers.monitors import moonraker_monitor
 monkeypatch.setattr(registry,'get_provider',lambda name:None)
 monkeypatch.setattr(moonraker_monitor,'engine',capture_case)
 instance=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor)
 instance.printer_id=9;instance.name='Synthetic';instance._current_job_db_id=None
 instance._job_started(SimpleNamespace(filename='Synthetic',total_layers=0,bed_target=0,nozzle_target=0))
 assert instance._current_job_db_id==7299
 with capture_case.connect() as conn:assert conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7299')).scalar_one() is None

@pytest.mark.parametrize('executor_kind',['connection','session'])
def test_insert_identity_helper_accepts_real_executor(capture_case,executor_kind):
 from core.db_compat import execute_insert_returning_id
 from sqlalchemy.orm import Session
 statement="INSERT INTO print_jobs(printer_id,job_name,status,started_at) VALUES(:printer,'Synthetic insert','running',CURRENT_TIMESTAMP)"
 if executor_kind=='connection':
  with capture_case.begin() as conn:identifier=execute_insert_returning_id(conn,statement,{'printer':9})
 else:
  with Session(capture_case) as session:
   identifier=execute_insert_returning_id(session,statement,{'printer':9});session.commit()
 with capture_case.connect() as conn:assert conn.execute(text('SELECT printer_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==9

def test_v2_report_bridge_creates_durable_attempt_without_mqtt_transport(capture_case,monkeypatch):
 from contextlib import contextmanager
 from types import SimpleNamespace
 import time
 from core.registry import registry
 from modules.printers.monitors import mqtt_printer,mqtt_job_lifecycle
 from modules.printers.telemetry.bambu import adapter,broker_policy
 from modules.printers.telemetry.events import BambuReportEvent
 from modules.printers.telemetry.bambu.raw import BambuPrintSection
 @contextmanager
 def database():
  conn=capture_case.raw_connection()
  try:yield conn
  finally:conn.close()
 monkeypatch.setattr(registry,'get_provider',lambda name:None)
 monkeypatch.setattr(mqtt_printer,'get_db',database)
 monkeypatch.setattr(mqtt_job_lifecycle,'get_db',database)
 with capture_case.begin() as conn:conn.execute(text('DELETE FROM print_jobs'))
 class LocalAdapter:
  def __init__(self,config,emitter):self.emitter=emitter
  def start(self):pass
 monkeypatch.setattr(adapter,'BambuTelemetryAdapter',LocalAdapter)
 monkeypatch.setattr(broker_policy,'resolve_bambu_broker_config',lambda *args,**kwargs:SimpleNamespace(host='127.0.0.1',port=1,use_tls=False))
 import uuid
 monitor=mqtt_printer.PrinterMonitor(9,'Synthetic','127.0.0.1',uuid.uuid4().hex,uuid.uuid4().hex)
 monitor._last_heartbeat=monitor._last_progress_update=monitor._last_spool_check=time.time()
 monitor._camera_discovered=True
 monkeypatch.setattr(monitor,'_recover_orphaned_jobs',lambda:None)
 monkeypatch.setattr(monitor,'_dispatch_alert',lambda **kwargs:None)
 monkeypatch.setattr(monitor,'_trigger_reschedule',lambda:None)
 assert monitor._connect_v2()
 event=BambuReportEvent(printer_id='monitor-9',ts=time.time(),section=BambuPrintSection(gcode_state='RUNNING',subtask_name='Synthetic',gcode_file='synthetic.gcode',total_layer_num=0))
 monitor._v2_adapter.emitter(event)
 assert monitor._current_job_id is not None
 with capture_case.connect() as conn:
  captured=json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':monitor._current_job_id}).scalar_one())
  assert any(x['spool_id']==7299 for x in captured['slots'])
 monitor._v2_adapter.emitter(event)
 with capture_case.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one()==1

@pytest.fixture
def mapped_capture_case(capture_case):
 with capture_case.begin() as conn:
  conn.execute(text('INSERT INTO spools(id,filament_id,remaining_weight_g,org_id) VALUES(7301,7299,1000,1)'))
  conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id) VALUES(9,2,7301)"))
  conn.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,print_file_id,ams_mapping) VALUES(7399,'Synthetic mapped',9,1,7399,'[0,1]')"))
  conn.execute(text("INSERT INTO print_files(id,filename,stored_path,org_id,filament_weight_grams,filaments_json) VALUES(7399,'Synthetic.3mf','/opaque/synthetic.3mf',1,80,:materials)"),{'materials':json.dumps([{'slot':0,'used_grams':20},{'slot':1,'used_grams':60}])})
  conn.execute(text('UPDATE print_jobs SET scheduled_job_id=7399 WHERE id=7299'))
 return capture_case

def test_explicit_material_plan_captured_with_estimate_provenance(mapped_capture_case):
 with mapped_capture_case.begin() as conn:assert capture(conn)
 plan=snapshot(mapped_capture_case)['material_plan']
 assert plan['state']=='complete' and plan['print_file_id']==7399
 assert plan['scheduled_job_id']==7399 and plan['org_id']==1
 assert [(x['spool_id'],x['estimated_grams'],x['quantity_provenance']) for x in plan['allocations']]==[(7299,20,'sliced_estimate'),(7301,60,'sliced_estimate')]
 with mapped_capture_case.begin() as conn:
  conn.execute(text("UPDATE jobs SET ams_mapping='[1,0]' WHERE id=7399"))
  conn.execute(text("UPDATE print_files SET filament_weight_grams=999,filaments_json='[]' WHERE id=7399"))
  assert not capture(conn)
 assert snapshot(mapped_capture_case)['material_plan']==plan

@pytest.mark.parametrize('change,reason',[
 ("UPDATE jobs SET ams_mapping='[0]' WHERE id=7399",'incomplete_mapping'),
 ("UPDATE jobs SET ams_mapping='[0,0]' WHERE id=7399",'invalid_mapping'),
 ("UPDATE print_files SET org_id=2 WHERE id=7399",'foreign_file'),
 ("UPDATE filament_slots SET assigned_spool_id=7300 WHERE printer_id=9 AND slot_number=2",'foreign_spool'),
 ("UPDATE jobs SET print_file_id=99999 WHERE id=7399",'missing_explicit_file'),
 ("UPDATE print_files SET filaments_json='not-json' WHERE id=7399",'invalid_material_metadata'),
])
def test_inconsistent_material_plan_persists_reason_without_guess(mapped_capture_case,change,reason):
 with mapped_capture_case.begin() as conn:
  conn.execute(text(change));assert capture(conn)
 plan=snapshot(mapped_capture_case)['material_plan']
 assert plan['state']=='unresolved' and plan['reason']==reason and plan['allocations']==[]
 with mapped_capture_case.connect() as conn:assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7299')).scalar_one()==1000

def test_spool_inserted_between_reads_cannot_bless_missing_start_identity(capture_case,monkeypatch):
 from types import SimpleNamespace
 from modules.archives import attempt_accounting
 # Model a preexisting legacy dangling assignment. Current FK enforcement
 # rejects creating it; disable enforcement only while seeding this fixture.
 with capture_case.connect() as conn:
  conn.exec_driver_sql('PRAGMA foreign_keys=OFF');conn.commit()
  conn.execute(text('UPDATE filament_slots SET assigned_spool_id=7499 WHERE printer_id=9 AND slot_number=1'));conn.commit()
  conn.exec_driver_sql('PRAGMA foreign_keys=ON');conn.commit()
 real_execute=attempt_accounting.execute
 inserted=[]
 def execute(conn,statement,parameters=()):
  result=real_execute(conn,statement,parameters)
  if statement.startswith('SELECT fs.slot_number,fs.assigned_spool_id'):
   rows=result.fetchall()
   real_execute(conn,'INSERT INTO spools(id,filament_id,org_id,remaining_weight_g) VALUES(7499,7299,2,1000)')
   inserted.append(True)
   return SimpleNamespace(fetchall=lambda:rows)
  return result
 monkeypatch.setattr(attempt_accounting,'execute',execute)
 with capture_case.begin() as conn:assert capture(conn)
 assert inserted==[True]
 assert snapshot(capture_case)['material_plan']['state']=='unresolved'
 assert snapshot(capture_case)['material_plan']['reason']=='missing_spool'

@pytest.mark.parametrize('change,reason',[
 ("UPDATE print_files SET filament_weight_grams=999 WHERE id=7399",'conflicting_material_quantities'),
 ("UPDATE print_files SET filament_weight_grams=-1 WHERE id=7399",'invalid_material_quantity'),
 ("UPDATE print_files SET filaments_json='[{\"used_grams\": -1},{\"used_grams\": 81}]' WHERE id=7399",'invalid_material_quantity'),
 ("UPDATE print_files SET filaments_json='[{\"used_grams\": NaN},{\"used_grams\": 60}]' WHERE id=7399",'invalid_material_quantity'),
])
def test_invalid_or_conflicting_quantity_evidence_never_becomes_accountable(mapped_capture_case,change,reason):
 with mapped_capture_case.begin() as conn:
  conn.execute(text(change));assert capture(conn)
 plan=snapshot(mapped_capture_case)['material_plan']
 assert plan['state']=='unresolved' and plan['reason']==reason and plan['allocations']==[]

def test_huge_json_integer_records_invalid_quantity_instead_of_aborting_start(mapped_capture_case):
 with mapped_capture_case.begin() as conn:
  conn.execute(text('UPDATE print_files SET filaments_json=:materials WHERE id=7399'),{'materials':json.dumps([{'slot':0,'used_grams':10**400},{'slot':1,'used_grams':60}])})
  assert capture(conn)
 assert snapshot(mapped_capture_case)['material_plan']['reason']=='invalid_material_quantity'
