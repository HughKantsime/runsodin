"""Current actual PostgreSQL org/global first-start callbacks; no physical commands."""
import os, stat, secrets, json, tempfile, hashlib, shutil
from pathlib import Path
private=Path(os.environ['COMPLETION_NATIVE_PASSWORD_FILE'])
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(private)
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from unittest.mock import patch
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
from core.schema import bootstrap_database
bootstrap_database(engine,Path('/source/backend'))
from core import auth,db as configured_db
from core.db import get_db
from core.audit_actor import register_request_session,clear_session
from core.middleware.audit_actor import AuditActorMiddleware
from core.registry import registry
from modules.orders.production import OrderFulfillmentService
from modules.organizations.services import EducationPolicyService
from modules.orders.routes.orders_crud import router as orders_router
from modules.inventory.routes.spools import router as spool_router
from modules.printers import dispatch,dispatch_acquisition
from core.attempt_start_transaction import start_transaction
from core.attempt_start_plan import prepare_start,create_start,finalize_start
from modules.archives.attempt_accounting import capture_attempt_spools
from core.event_bus import InMemoryEventBus
from core.monitor_bootstrap import bootstrap_monitor_authority
from core import events as event_types
real_bus=InMemoryEventBus()
event_bus_module=None


from contextlib import contextmanager
from types import SimpleNamespace
from core.db_utils import _ConnectionAdapter
from core import event_bus
from modules.printers import smart_plug
from modules.printers.monitors import mqtt_job_lifecycle,moonraker_monitor
from modules.notifications import job_events
from core.crypto import encrypt
from core import crypto
from cryptography.fernet import Fernet
import zipfile
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
root=Path(tempfile.mkdtemp(prefix='owned-callback-'))
configured_db.engine=engine;smart_plug.engine=engine
from core.event_bus import InMemoryEventBus
from core.monitor_bootstrap import bootstrap_monitor_authority
from core import events as event_types
event_bus._bus=InMemoryEventBus();real_bus=event_bus.get_event_bus()
class InertTimer:
 def __init__(self,*a,**k):pass
 def start(self):pass
 def cancel(self):pass
smart_plug.threading.Timer=InertTimer
bootstrap_monitor_authority()
assert len(real_bus._handlers[event_types.JOB_COMPLETED])==1
from modules.notifications import alert_dispatch,event_dispatcher
from modules.archives import archive
for module,names in [(alert_dispatch,('dispatch_alert','_start_bed_cooled_monitor')),(event_dispatcher,('dispatch_alert','record_error','increment_care_counters'))]:
 for name in names:setattr(module,name,lambda *a,**k:None)
archive.create_print_archive=lambda *a,**k:None
job_events.dispatch_alert=lambda *a,**k:None
job_events.increment_care_counters=lambda *a,**k:None
job_events._start_bed_cooled_monitor=lambda *a,**k:None
records=[];start_records=[]
def collect(event):
 record={'event':event,'state':None,'error':None,'success':False,'claim_count':None}
 records.append(record)
 try:
  with engine.connect() as observer:
   record['state']=observer.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':event.data['print_job_id']}).scalar_one()
   record['claim_count']=observer.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=:id'),{'id':event.data['printer_id']}).scalar_one()
  assert record['state']=='completed'
  record['success']=True
 except Exception as error:
  record['error']=type(error).__name__;raise
def collect_start(event):
 record={'event':event,'success':False,'error':None};start_records.append(record)
 try:
  with engine.connect() as observer:assert observer.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':event.data['print_job_id']}).scalar_one() is not None
  record['success']=True
 except Exception as error:record['error']=type(error).__name__;raise
def assert_records(values):
 assert values and all(r['success'] and r['error'] is None for r in values),'Recorded observer timing failure'
real_bus.subscribe(event_types.JOB_COMPLETED,collect)
real_bus.subscribe(event_types.JOB_STARTED,collect_start)

with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned school',TRUE)")
 for uid,user,org in [(71,'platform',None),(72,'organization',1)]:
  c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(:id,:user,:hash,'admin',TRUE,:org)"),{'id':uid,'user':user,'hash':auth.hash_password(secrets.token_urlsafe(32)),'org':org})
 c.exec_driver_sql("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(80,'Owned','Native','PLA','ffffff')")
 c.exec_driver_sql("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('global',0,'USD'),('org',1,'USD')")
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(orders_router,prefix='/api');app.include_router(spool_router,prefix='/api')
def database(request:Request):
 with Session(engine,autoflush=False) as db:
  register_request_session(db,request)
  try:yield db
  finally:clear_session(db)
app.dependency_overrides[get_db]=database
@contextmanager
def raw_database():
 raw=engine.raw_connection()
 try:yield _ConnectionAdapter(raw,True)
 finally:raw.close()
checks=[]
for n,(owner,org,username,protocol) in enumerate((owner,org,user,protocol) for owner,org,user in [('org',1,'organization'),('global',None,'platform')] for protocol in ['bambu','prusalink','moonraker','prusalink','bambu','prusalink','moonraker']):
 pid=100+n;model=200+n;product=300+n;fid=400+n
 payload=root/(str(pid)+('.3mf' if protocol=='bambu' else '.gcode'))
 if protocol=='bambu':
  with zipfile.ZipFile(payload,'w') as z:z.writestr('Metadata/plate_1.gcode','G90\n')
 else:payload.write_text('; owned inert\nG90\n')
 credential=encrypt('SYNTHETIC'+str(pid)+'|'+secrets.token_hex(16)) if protocol=='bambu' else ''
 with engine.begin() as c:
  c.execute(text("INSERT INTO printers(id,name,is_active,api_type,api_host,api_key,org_id,gcode_state,bed_x_mm,bed_y_mm,nozzle_diameter) VALUES(:id,:name,TRUE,:protocol,:host,:key,:org,'IDLE',256,256,.4)"),{'id':pid,'name':'Owned '+str(pid),'protocol':protocol,'host':'127.0.0.'+str(n+1),'key':credential,'org':org})
  c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(:id,1,'PLA')"),{'id':pid})
  c.execute(text("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(:id,:name,:org,1,1,'PLA')"),{'id':model,'name':'Owned model '+str(n),'org':org})
  c.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(:id,:name,:org,20)"),{'id':product,'name':'Owned product '+str(n),'org':org})
  c.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(:product,:model,1)'),{'product':product,'model':model})
  c.execute(text('INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(:id,:model,:name,:name,:path,:org)'),{'id':fid,'model':model,'name':payload.name,'path':str(payload),'org':org})
 headers={'Authorization':'Bearer '+auth.create_access_token({'sub':username})}
 with TestClient(app,raise_server_exceptions=False) as client:
  r=client.post('/api/orders',headers=headers,json={'order_number':'OWNED-CALLBACK-'+str(n),'items':[{'product_id':product,'quantity':1,'unit_price':20}]});assert r.status_code==200,('order',owner,protocol,r.status_code)
  order=r.json()['id'];r=client.post(f'/api/orders/{order}/schedule',headers=headers);assert r.status_code==200,('schedule',r.status_code)
  r=client.post('/api/spools',headers=headers,json={'filament_id':80,'purchase_evidence':{'currency':'USD','price':'12.99','net_weight_g':'750.25'}});assert r.status_code==200,('spool',r.status_code)
  spool=r.json()['id']
 with engine.begin() as c:
  job=c.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'),{'id':order}).scalar_one()
  c.execute(text('UPDATE jobs SET printer_id=:pid,print_file_id=:file,plate_index=0,payload_sha256=:hash WHERE id=:id'),{'pid':pid,'file':fid,'hash':hashlib.sha256(payload.read_bytes()).hexdigest(),'id':job})
  c.execute(text('UPDATE filament_slots SET assigned_spool_id=:spool WHERE printer_id=:pid'),{'spool':spool,'pid':pid})
  if org is None:assert c.execute(text('SELECT count(*) FROM cost_global_order_origins g JOIN orders o ON o.economic_uid=g.order_uid WHERE o.id=:id'),{'id':order}).scalar_one()==1
 names=[]
 def sender(*args,**kwargs):names.append(args[2]);return True,'Owned physical sender stub'
 with patch.object(dispatch,'_dispatch_'+protocol,sender),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
  result=dispatch.dispatch_job(pid,job);assert result[0],('dispatch',owner,protocol)
 events=[]
 prior_starts=len(start_records)
 def callback():
  if protocol=='bambu':return mqtt_job_lifecycle.record_job_started(pid,'Owned',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode'},lambda **kw:None,lambda:None)[0]
  if protocol=='prusalink':return job_events.job_started(pid,names[0])
  monitor=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor)
  monitor.printer_id=pid;monitor.name='Owned';monitor._current_job_db_id=None
  monitor._try_auto_link=lambda *a:(_ for _ in ()).throw(AssertionError('Reserved fuzzy link'))
  monitor._job_started(SimpleNamespace(filename=names[0],total_layers=1,bed_target=60,nozzle_target=200))
  return monitor._current_job_db_id
 def facts():
  with engine.connect() as c:
   return (tuple(c.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings']),c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job}).scalar_one(),c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':job}).scalar_one())
 with patch.object(job_events,'get_db',raw_database),patch.object(mqtt_job_lifecycle,'get_db',raw_database),patch.object(moonraker_monitor,'engine',engine):
  baseline=facts()
  with engine.begin() as c:
   c.exec_driver_sql("CREATE FUNCTION owned_callback_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned deferred callback' USING ERRCODE='23514'; END $$")
   c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_callback_fault AFTER INSERT ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_callback_fault()')
  assert callback() is None,('deferred callback returned success',protocol)
  assert facts()==baseline and events==[],('outer rollback',owner,protocol)
  with engine.begin() as c:
   c.exec_driver_sql('DROP TRIGGER owned_callback_fault ON print_jobs');c.exec_driver_sql('DROP FUNCTION owned_callback_fault()')
  identifier=callback();assert identifier is not None,('callback',owner,protocol)
  with engine.connect() as c:
   row=c.execute(text('SELECT p.economic_uid,p.scheduled_job_id,l.state,l.owner_scope,l.owner_key,p.spool_snapshot FROM print_jobs p JOIN cost_source_lifetimes l ON l.birth_nonce=p.economic_birth_nonce WHERE p.id=:id'),{'id':identifier}).one()
   assert row[0] and tuple(row[1:5])==(job,'known',owner,org or 0)
   snapshot=row[5];value=json.loads(snapshot)['unit_valuation'];assert value['state']=='known' and value['target']['owner_scope']==owner
   assert value['sources'][0]['price_decimal']=='12.99' and value['sources'][0]['net_weight_g_decimal']=='750.25'
   assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':job}).scalar_one()==identifier
  events[:]=[r['event'] for r in start_records[prior_starts:]]
  assert_records(start_records[prior_starts:])
  assert len(events)==1,('fresh event',protocol,len(events))
  before=facts();assert callback()==identifier
  assert facts()==before and len(start_records)==prior_starts+1,('resume repeated event/birth',protocol)
  with engine.connect() as c:assert c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==snapshot

 terminal_status='failed' if n%7>=4 else 'completed'
 zone='America/New_York' if n%7==3 else 'UTC'
 @contextmanager
 def timezone_terminal_database():
  with raw_database() as connection:
   connection.cursor().execute("SET LOCAL TIME ZONE '"+zone+"'")
   yield connection
 def finish(status=None):
  status=terminal_status if status is None else status
  if protocol=='bambu':return mqtt_job_lifecycle.record_job_ended(pid,'Owned',identifier,job,status,{'subtask_name':names[0]}, {},lambda **kw:None)
  if protocol=='moonraker':
   monitor=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor)
   monitor.printer_id=pid;monitor.name='Owned';monitor._current_job_db_id=identifier;monitor._last_filename=names[0]
   return monitor._job_ended(status,SimpleNamespace(filename=names[0],filament_used_mm=0,error_message=None))
  return job_events.job_completed(pid,identifier,success=status=='completed',duration_seconds=12.5)
 def terminal_facts():
  with engine.connect() as c:return (tuple(c.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=:id'),{'id':identifier}).one()),tuple(c.execute(text('SELECT status,actual_start,actual_end FROM jobs WHERE id=:id'),{'id':job}).one()),c.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=:pid'),{'pid':pid}).scalar_one())
 with patch.object(job_events,'get_db',timezone_terminal_database),patch.object(mqtt_job_lifecycle,'get_db',raw_database),patch.object(moonraker_monitor,'engine',engine):
  initial=terminal_facts();prior=len(records)
  if n==0:
   from core.committed_terminal_events import publish_committed_completion
   assert publish_committed_completion(pid,identifier,job) is True
   bad=records[-1];assert bad['state']=='running' and bad['success'] is False and bad['error']=='AssertionError'
   try:assert_records([bad])
   except AssertionError:pass
   else:raise AssertionError('running-state observer falsely qualified')
   print('outside-negative-control running-state remainsRED despitepublisherTrue',flush=True)
   prior=len(records)
  with engine.connect() as observer:expected_job_name=observer.execute(text('SELECT job_name FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()
  with engine.begin() as c:
   c.exec_driver_sql("CREATE FUNCTION owned_terminal_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.status='running' AND NEW.status IN ('completed','failed') THEN RAISE EXCEPTION 'owned deferred terminal' USING ERRCODE='23514'; END IF; RETURN NEW; END $$")
   c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_terminal_fault AFTER UPDATE ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_terminal_fault()')
  finish();assert terminal_facts()==initial and len(records)==prior,('outer rollback/noevent',owner,protocol)
  with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_terminal_fault ON print_jobs');c.exec_driver_sql('DROP FUNCTION owned_terminal_fault()')
  with patch.object(alert_dispatch,'dispatch_alert',side_effect=RuntimeError('owned optional output failure')) if n==0 else patch.object(alert_dispatch,'dispatch_alert',lambda *a,**k:None):
   finish()
  assert len(records)==prior+(1 if terminal_status=='completed' else 0)
  if terminal_status=='completed':
   assert_records(records[prior:]);assert records[-1]['claim_count']==0
   event=records[-1]['event'];assert event.data['scheduled_job_id']==job and event.source_module==('notifications' if protocol=='prusalink' else 'printers')
   if protocol=='prusalink':assert event.data['job_name']==expected_job_name and event.data['duration_seconds']==12.5
  terminal=terminal_facts();assert terminal[0][0]==terminal[1][0]==terminal_status and terminal[1][1]==initial[1][1]
  finish();finish('failed' if terminal_status=='completed' else 'completed');assert terminal_facts()==terminal and len(records)==prior+(1 if terminal_status=='completed' else 0)
  if protocol=='prusalink':
   from core.dispatch_observations import _utc
   assert _utc(terminal[0][1])==_utc(terminal[1][2])
 checks.append(owner+'/'+protocol+'/'+terminal_status+'/'+zone+': actual own terminal producer, live standard bus/fresh observer, deferred rollback noevent, exactIDs/source/metadata, normalclaimretirement and duplicates')
 print('completion-case-pass '+owner+'/'+protocol+'/'+zone,flush=True)
shutil.rmtree(root)
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
fanout=[]
def forbidden(*a,**kw):fanout.append(True);raise AssertionError('Education generic event/alert fanout')
def state(pid):
 with engine.connect() as c:
  upload=uploads[pid];job=upload['job_id']
  return (tuple(c.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':upload['id']}).one()),c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job}).scalar_one(),tuple(c.execute(text('SELECT state,authority_revision FROM education_monitor_claims WHERE job_id=:id'),{'id':job}).one()),c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':job}).scalar_one(),tuple(c.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings','education_audit_events']))
def callback(pid):
 if pid==9:return mqtt_job_lifecycle.record_job_started(pid,'Owned',{'subtask_name':names[pid],'gcode_file':'Metadata/plate_1.gcode'},forbidden,forbidden)[0]
 return job_events.job_started(pid,names[pid])

with patch.object(job_events,'get_db',database),patch.object(mqtt_job_lifecycle,'get_db',database),patch.object(moonraker_monitor,'engine',engine),patch.object(job_events,'dispatch_alert',forbidden),patch.object(job_events,'increment_care_counters',forbidden):
 for pid in (9,10):
  identifier=callback(pid);assert identifier is not None
  prior=len(records);assert not fanout
  with engine.begin() as c:
   c.exec_driver_sql("CREATE FUNCTION owned_edu_terminal_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='completed' THEN RAISE EXCEPTION 'owned deferred edu terminal' USING ERRCODE='23514'; END IF; RETURN NEW; END $$")
   c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_edu_terminal_fault AFTER UPDATE ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_edu_terminal_fault()')
  def terminal():
   if pid==9:return mqtt_job_lifecycle.record_job_ended(pid,'Owned',identifier,uploads[pid]['job_id'],'completed',{'subtask_name':names[pid]}, {},forbidden)
   return job_events.job_completed(pid,identifier,success=True,duration_seconds=45)
  before=state(pid);terminal();assert state(pid)==before and len(records)==prior
  with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_edu_terminal_fault ON print_jobs');c.exec_driver_sql('DROP FUNCTION owned_edu_terminal_fault()')
  terminal();terminal();assert len(records)==prior+1
  assert_records(records[prior:]);assert not fanout
  assert records[-1]['event'].data['scheduled_job_id']==uploads[pid]['job_id']
  assert records[-1]['event'].source_module==('printers' if pid==9 else 'notifications')
  with engine.connect() as c:assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':uploads[pid]['id']}).scalar_one()=='completed'
  checks.append('Education '+('MQTT' if pid==9 else 'notifications')+': genuine upload/approve/schedule/dispatch/start/terminal, standardbus outsideobserver, deferredrollback and nogenericfanout')
 # Wrong-protocol start remains quarantined; do not invent Moonraker EDU dispatch.
 moon=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor);moon.printer_id=11;moon.name='Owned';moon._current_job_db_id=None
 before=state(11);prior_complete=len(records);prior_start=len(start_records)
 moon._job_started(SimpleNamespace(filename=names[11],total_layers=1,bed_target=60,nozzle_target=200))
 quarantined=moon._current_job_db_id;assert quarantined is not None
 with engine.connect() as observer:
  row=observer.execute(text('SELECT scheduled_job_id,economic_uid,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':quarantined}).one()
  assert row[0] is None and row[1] is None and row[2]
  assert observer.execute(text('SELECT state FROM cost_source_lifetimes WHERE birth_nonce=:nonce'),{'nonce':row[2]}).scalar_one()=='unresolved'
 assert state(11)[:4]==before[:4] and len(records)==prior_complete and len(start_records)==prior_start
 checks.append('WrongMoonraker/Bambu quarantine is freshunresolved/unlinked, no authoritymutation/start/completionevent')
# Standard bus swallows deliberate observer errors: assert failure outside it.
from core.committed_terminal_events import publish_committed_completion
negative=[]
def broken(event):
 record={'success':False,'error':None};negative.append(record)
 try:raise RuntimeError('owned fresh observer lookup fault')
 except Exception as error:record['error']=type(error).__name__;raise
real_bus.subscribe(event_types.JOB_COMPLETED,broken)
assert publish_committed_completion(10,identifier,uploads[10]['job_id']) is True
assert len(negative)==1 and negative[0]['error']=='RuntimeError'
print('outside-negative-control lookup-failure remainsRED despitepublisherTrue',flush=True)
try:assert_records(negative)
except AssertionError:pass
else:raise AssertionError('swallowed observer failure falsely qualified')
real_bus.unsubscribe(event_types.JOB_COMPLETED,broken)
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose()
print(json.dumps({'passed':True,'checks':checks,'scope':'Actual current PG own3generic terminal producer families with org/global dispatch/start fixtures, notifications UTC/nonUTC; genuine EDU Bambu MQTT/generic flows and wrongMoonraker quarantine; live standardbus outsideobserver, no hardware/externaldelivery/COGS/fullpowerclaim','owned_fixture_removed':not root.exists()}),flush=True)
print('committed-completion-native-pass',flush=True)
