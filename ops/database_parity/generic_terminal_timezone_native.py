"""Current actual PostgreSQL org/global first-start callbacks; no physical commands."""
import os, stat, secrets, json, tempfile, hashlib, shutil
from pathlib import Path
private=Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
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
registry._providers['OrderFulfillmentProvider']=OrderFulfillmentService()
registry._providers['EducationPolicyProvider']=EducationPolicyService()

from contextlib import contextmanager
from types import SimpleNamespace
from core.db_utils import _ConnectionAdapter
from core import event_bus
from modules.printers import smart_plug
from modules.printers.monitors import mqtt_job_lifecycle,moonraker_monitor
from modules.notifications import job_events
from core.crypto import encrypt
from cryptography.fernet import Fernet
import zipfile
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
root=Path(tempfile.mkdtemp(prefix='owned-callback-'))
configured_db.engine=engine;smart_plug.engine=engine
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
for n,(owner,org,username,protocol) in enumerate((owner,org,user,protocol) for owner,org,user in [('org',1,'organization'),('global',None,'platform')] for protocol in ['bambu','prusalink','moonraker']):
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
 def committed(event):
  with engine.connect() as observer:
   assert observer.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':event.data['print_job_id']}).scalar_one() is not None
  events.append(event)
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
 with patch.object(job_events,'get_db',raw_database),patch.object(mqtt_job_lifecycle,'get_db',raw_database),patch.object(moonraker_monitor,'engine',engine),patch.object(job_events,'get_event_bus',lambda:SimpleNamespace(publish=committed)),patch.object(event_bus,'get_event_bus',lambda:SimpleNamespace(publish=committed)):
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
  assert len(events)==1,('fresh event',protocol,len(events))
  before=facts();assert callback()==identifier
  assert facts()==before and len(events)==1,('resume repeated event/birth',protocol)
  with engine.connect() as c:assert c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==snapshot
 @contextmanager
 def timezone_terminal_database():
  with raw_database() as connection:
   connection.cursor().execute("SET LOCAL TIME ZONE 'America/New_York'")
   yield connection
 with patch.object(job_events,'get_db',timezone_terminal_database),patch.object(job_events,'dispatch_alert',lambda **kw:None),patch.object(job_events,'increment_care_counters',lambda *a:None),patch.object(job_events,'get_event_bus',lambda:SimpleNamespace(publish=lambda event:None)):
  def terminal_facts():
   with engine.connect() as c:
    return (tuple(c.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=:id'),{'id':identifier}).one()),tuple(c.execute(text('SELECT status,actual_start,actual_end FROM jobs WHERE id=:id'),{'id':job}).one()),c.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=:pid'),{'pid':pid}).scalar_one())
  original_terminal=terminal_facts();assert original_terminal[0][0]=='running' and original_terminal[2]==1
  def finish():
   if protocol=='prusalink':return job_events.job_cancelled(pid,identifier)
   return job_events.job_completed(pid,identifier,success=protocol!='moonraker')
  with engine.begin() as c:
   c.exec_driver_sql("CREATE FUNCTION owned_terminal_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.status='running' AND NEW.status IN ('completed','failed','cancelled') THEN RAISE EXCEPTION 'owned deferred terminal' USING ERRCODE='23514'; END IF; RETURN NEW; END $$")
   c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_terminal_fault AFTER UPDATE ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_terminal_fault()')
  finish()
  assert terminal_facts()==original_terminal,('terminal deferred rollback status/time/claim',owner,protocol)
  with engine.begin() as c:
   c.exec_driver_sql('DROP TRIGGER owned_terminal_fault ON print_jobs');c.exec_driver_sql('DROP FUNCTION owned_terminal_fault()')
  finish();terminal_state=terminal_facts()
  expected='cancelled' if protocol=='prusalink' else 'failed' if protocol=='moonraker' else 'completed'
  assert terminal_state[0][0]==expected and terminal_state[1][0]==expected
  with engine.connect() as c:
   c.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
   comparison=c.execute(text('SELECT pg_typeof(p.ended_at)::text,pg_typeof(j.actual_end)::text,p.ended_at IS NOT DISTINCT FROM j.actual_end FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:id'),{'id':identifier}).one()
  print('terminal-time-type-proof '+json.dumps({'physical_type':comparison[0],'job_type':comparison[1],'sql_utc_equal':comparison[2]}),flush=True)
  from core.dispatch_observations import _utc
  assert terminal_state[0][1] is not None and comparison[2] is True
  assert _utc(terminal_state[0][1])==_utc(terminal_state[1][2])
  assert terminal_state[1][1]==original_terminal[1][1] and terminal_state[2]==0,('exact terminal binding/claim retirement',owner,protocol)
  job_events.job_completed(pid,identifier,success=expected!='completed');job_events.job_cancelled(pid,identifier)
  assert terminal_facts()==terminal_state,('late terminal immutable',owner,protocol)
 print('terminal-case-pass '+owner+'/'+protocol,flush=True)
 checks.append(owner+'/'+protocol+': signed order/schedule/025 source, actual dispatch/callback known exact unit capture, deferred outer rollback, fresh committed START event and no-recapture/start replay; generic terminal deferred rollback/retry exact UTC end, preserved start, claim retirement and late duplicate refusal')
 print('callback-case-pass '+owner+'/'+protocol,flush=True)
print(json.dumps({'passed':True,'checks':checks,'scope':'Actual current PostgreSQL org/global starts then generic terminal events with exact end/start, actual deferred rollback/claim retention, retry retirement and late duplicates; physical senders/idle telemetry/notification delivery stubbed; no hardware, EDU callback native, measured consumption or COGS certification'}),flush=True)
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose();print('generic-terminal-timezone-native-pass',flush=True)
