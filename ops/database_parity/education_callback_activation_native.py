"""Actual PostgreSQL Education services and activated callbacks; no physical sender."""
import os,stat,json,hashlib,tempfile,zipfile
from pathlib import Path
os.environ['JWT_SECRET_KEY']='owned-queue-native-signing-key-only'
private=Path(os.environ['EDU_CALLBACK_NATIVE_PASSWORD_FILE'])
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(private)
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from fastapi import FastAPI
from fastapi.testclient import TestClient
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
from core.schema import bootstrap_database
bootstrap_database(engine,Path('/source/backend'))
from core import auth,db as configured_db
from core.db import get_db
from cryptography.fernet import Fernet
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
from core import crypto
from core.attempt_start_transaction import start_transaction
from core.attempt_start_plan import prepare_start,create_start,finalize_start,_fresh_capture_context
from core.economic_source_creation import SourceCreationError
from core.registry import registry
from modules.orders.production import OrderFulfillmentService
from modules.organizations.services import EducationPolicyService
from modules.organizations.education_policy import claim_monitor_observation
from modules.printers import dispatch,dispatch_acquisition
from unittest.mock import patch
registry._providers['OrderFulfillmentProvider']=OrderFulfillmentService();registry._providers['EducationPolicyProvider']=EducationPolicyService()

import io,uuid,shutil,secrets
from datetime import datetime,timezone
from contextlib import contextmanager
from types import SimpleNamespace
from core.db_utils import _ConnectionAdapter
from core import event_bus
from modules.printers import smart_plug
from modules.printers.monitors import mqtt_job_lifecycle,moonraker_monitor
from modules.notifications import job_events
from modules.organizations.education_submission_service import process_sliced_3mf_submission
from modules.organizations.education_review_service import approve_submission
from modules.organizations.education_schemas import SubmissionApproval
from modules.jobs.scheduler import Scheduler
configured_db.engine=engine;smart_plug.engine=engine
root=Path(tempfile.mkdtemp(prefix='owned-edu-callback-'))
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned school',TRUE)")
 c.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'student','owned','viewer',TRUE,1),(2,'teacher','owned','viewer',TRUE,1),(3,'admin','owned','admin',TRUE,1)")
 c.exec_driver_sql("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(7,1,'owned','owned','Owned','OWN',3)")
 c.exec_driver_sql("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,7,1,'student','active',3),(1,7,2,'manager','active',3)")
 for pid in (9,10,11):
  c.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,api_host,api_key,org_id,shared,bed_x_mm,bed_y_mm,nozzle_diameter,gcode_state) VALUES(:id,:name,'Bambu Lab P1S','P1S',TRUE,'bambu','127.0.0.1',:key,1,FALSE,256,256,.4,'IDLE')"),{'id':pid,'name':'owned-'+str(pid),'key':crypto.encrypt('owned-serial-'+str(pid)+'|'+secrets.token_hex(16))})
  c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(:id,1,'PLA')"),{'id':pid})
  c.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,granted_by) VALUES(1,7,:id,3)"),{'id':pid})
buffer=io.BytesIO()
with zipfile.ZipFile(buffer,'w') as z:
 z.writestr('Metadata/slice_info.config','<config><plate><metadata key="prediction" value="3600"/><metadata key="weight" value="12.5"/><metadata key="nozzle_diameters" value="0.4"/><metadata key="printer_model_id" value="BL-P001"/><filament id="1" type="PLA" color="#FFFFFF" used_m="4" used_g="12.5"/></plate></config>')
 z.writestr('Metadata/project_settings.config',json.dumps({'printer_model':'Bambu Lab P1S','nozzle_diameter':['0.4'],'filament_type':['PLA'],'bed_shape':'0x0,256x0,256x256,0x256'}))
 z.writestr('Metadata/plate_1.json',json.dumps({'nozzle_diameter':.4}))
 z.writestr('Metadata/plate_1.gcode','; owned inert\nG28\n')
 z.writestr('3D/3dmodel.model','<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><metadata name="Title">Owned class</metadata><resources/><build/></model>')
def principal(uid):return {'id':uid,'username':'student' if uid==1 else 'teacher','role':'viewer','group_id':1,'is_active':True,'_auth_kind':'session_jwt'}
uploads={};names={}
for pid in (9,10,11):
 with Session(engine) as db:
  upload=process_sliced_3mf_submission(db,source=io.BytesIO(buffer.getvalue()),original_filename='owned.3mf',operation_id=str(uuid.uuid4()),principal=principal(1),cost_center_id=7,storage_root=root/'uploads')
  approval=approve_submission(db,submission_id=upload['id'],body=SubmissionApproval(revision=upload['lifecycle_revision'],printer_id=pid,command_id=uuid.uuid4()),principal=principal(2))
  assert approval['compatibility']['compatible']
  result=Scheduler().run(db,start_date=datetime.now(timezone.utc));assert result.success and result.scheduled_count==1,('schedule',result.scheduled_count)
  row=db.execute(text('SELECT s.status,j.status,j.printer_id FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'),{'id':upload['id']}).one();assert tuple(row)==('scheduled','scheduled',pid)
  db.rollback();uploads[pid]=upload
 def sender(*args,**kwargs):names[pid]=args[2];return True,'Owned external sender stub'
 with patch.object(dispatch,'_dispatch_bambu',sender),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
  if pid==9:
   with patch.object(dispatch,'_confirm_education_dispatch',lambda *a:{'transitioned':False}):assert dispatch.dispatch_job(pid,upload['job_id'])[0] is False
  else:assert dispatch.dispatch_job(pid,upload['job_id'])[0]
@contextmanager
def database():
 raw=engine.raw_connection()
 try:yield _ConnectionAdapter(raw,True)
 finally:raw.close()
def forbidden(*a,**kw):raise AssertionError('Education generic event/alert fanout')
def state(pid):
 with engine.connect() as c:
  upload=uploads[pid];job=upload['job_id']
  return (tuple(c.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':upload['id']}).one()),c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job}).scalar_one(),tuple(c.execute(text('SELECT state,authority_revision FROM education_monitor_claims WHERE job_id=:id'),{'id':job}).one()),c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':job}).scalar_one(),tuple(c.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings','education_audit_events']))
def callback(pid):
 if pid==9:return mqtt_job_lifecycle.record_job_started(pid,'Owned',{'subtask_name':names[pid],'gcode_file':'Metadata/plate_1.gcode'},forbidden,forbidden)[0]
 return job_events.job_started(pid,names[pid])
checks=[]
terminal_events=[]
observer_records=[]
def record_observer(records,event,read_state):
 record={'event':event,'success':False,'state':None,'error':None}
 records.append(record)
 try:
  record['state']=read_state(event)
  assert record['state']=='completed', 'Fresh observer must see completed at delivery'
  record['success']=True
 except Exception as error:
  record['error']=type(error).__name__
  raise

def assert_observers(records):
 assert all(r['success'] is True and r['state']=='completed' and r['error'] is None for r in records), 'Recorded observer timing failure'

def observe_event(event):
 # Count every delivery attempt before assertions; runtime may catch errors.
 terminal_events.append(event)
 def read_state(delivery):
  from core import events
  assert delivery.event_type==events.JOB_COMPLETED and delivery.source_module==('printers' if delivery.data['printer_id']==9 else 'notifications')
  assert delivery.data['status']=='completed' and delivery.data['success'] is True
  with engine.connect() as observer:
   return observer.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':delivery.data['print_job_id']}).scalar_one()
 record_observer(observer_records,event,read_state)
with patch.object(job_events,'get_db',database),patch.object(mqtt_job_lifecycle,'get_db',database),patch.object(moonraker_monitor,'engine',engine),patch.object(job_events,'get_event_bus',lambda:SimpleNamespace(publish=observe_event)),patch.object(event_bus,'get_event_bus',lambda:SimpleNamespace(publish=observe_event)),patch.object(job_events,'dispatch_alert',forbidden),patch.object(job_events,'increment_care_counters',forbidden):
 for pid in (9,10):
  for table,mode in [('education_audit_events','audit'),('print_jobs','deferred')]:
   before=state(pid)
   with engine.begin() as c:
    c.exec_driver_sql("CREATE FUNCTION owned_edu_callback_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned callback failure' USING ERRCODE='23514'; END $$")
    if mode=='audit':c.exec_driver_sql('CREATE TRIGGER owned_edu_callback_fault BEFORE INSERT ON education_audit_events FOR EACH ROW EXECUTE FUNCTION owned_edu_callback_fault()')
    else:c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_edu_callback_fault AFTER INSERT ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_edu_callback_fault()')
   prior_events=len(terminal_events)
   assert callback(pid) is None,('fault returned success',pid,mode)
   assert len(terminal_events)==prior_events, 'Failed outer transaction must not deliver an event'
   assert state(pid)==before,('whole outer rollback',pid,mode)
   with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_edu_callback_fault ON '+table);c.exec_driver_sql('DROP FUNCTION owned_edu_callback_fault()')
  before_events=len(terminal_events)
  identifier=callback(pid);assert identifier is not None,('positive callback',pid)
  current=state(pid);assert current[0][0]=='printing' and current[2][0]=='running' and current[3]==identifier
  with engine.connect() as c:
   row=c.execute(text('SELECT scheduled_job_id,economic_uid,economic_birth_nonce,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
   assert row[0]==uploads[pid]['job_id'] and row[1] is None and row[2] and row[3]
   assert c.execute(text('SELECT state FROM cost_source_lifetimes WHERE birth_nonce=:nonce'),{'nonce':row[2]}).scalar_one()=='unresolved'
   snapshot=row[3];assert json.loads(snapshot)['unit_valuation']['state']!='known'
  assert len(terminal_events)==before_events, 'Education start must not publish generic start'
  job_events._active_print_jobs.clear()
  assert callback(pid)==identifier and state(pid)==current
  with engine.connect() as c:assert c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==snapshot
  if pid==9:
   for _ in range(2):assert mqtt_job_lifecycle.record_job_ended(pid,'Owned',identifier,uploads[pid]['job_id'],'completed',{'subtask_name':names[pid]}, {},forbidden)==uploads[pid]['job_id']
  else:
   for _ in range(2):job_events.on_print_complete(pid,names[pid],duration_seconds=45)
  assert len(terminal_events)==before_events+1, 'Only one committed completion for duplicate terminal'
  assert len(observer_records)==len(terminal_events)
  assert_observers(observer_records)
  from core import events
  assert terminal_events[-1].event_type==events.JOB_COMPLETED and terminal_events[-1].source_module==('printers' if pid==9 else 'notifications')
  assert terminal_events[-1].data['status']=='completed' and terminal_events[-1].data['success'] is True
  assert terminal_events[-1].data['print_job_id']==identifier and terminal_events[-1].data['scheduled_job_id']==uploads[pid]['job_id']
  with engine.connect() as c:
   assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':uploads[pid]['id']}).scalar_one()=='completed'
   assert c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':uploads[pid]['job_id']}).scalar_one()=='completed'
   assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()=='completed'
   assert c.execute(text('SELECT count(*) FROM education_monitor_claims WHERE job_id=:id'),{'id':uploads[pid]['job_id']}).scalar_one()==0
  checks.append(('MQTT reserved recovery' if pid==9 else 'generic-events awaiting confirmation')+': actual upload/approval/schedule/dispatch/callback/capture, audit and deferred full rollback, no generic fanout, restart replay and idempotent terminal')
  print('education-callback-case-pass '+str(pid),flush=True)
 # Real Bambu dispatch cannot be financial/operational authority for Moonraker.
 before=state(11)
 monitor=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor);monitor.printer_id=11;monitor.name='Owned wrong adapter';monitor._current_job_db_id=None
 monitor._job_started(SimpleNamespace(filename=names[11],total_layers=1,bed_target=60,nozzle_target=200))
 identifier=monitor._current_job_db_id;assert identifier is not None
 with engine.connect() as c:
  assert c.execute(text('SELECT scheduled_job_id,economic_uid FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==(None,None)
 assert state(11)[:4]==before[:4]
 assert len(terminal_events)==2
 checks.append('Nonmatching Moonraker on actual Bambu dispatch quarantine preserves teacher/universal authority')
print(json.dumps({'passed':True,'checks':checks,'scope':'Actual PostgreSQL EDU service upload/teacher approval/scheduler and Bambu dispatch through MQTT/generic callbacks; standalone unresolved sources/captured unpriced snapshot; synthetic principals, no HTTP auth/license claim, physical sender/idle telemetry stubbed; no commerce valuation, COGS or hardware'}),flush=True)
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose();print('education-callback-activation-native-pass',flush=True)
