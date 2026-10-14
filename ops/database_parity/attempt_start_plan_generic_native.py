"""Owned real PostgreSQL company-origin acceptance; never dispatches."""
import os,stat,json,hashlib,tempfile,zipfile
from pathlib import Path
os.environ['JWT_SECRET_KEY']='owned-queue-native-signing-key-only'
private=Path(os.environ['PLAN_NATIVE_PASSWORD_FILE'])
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
from core.attempt_start_transaction import start_transaction
from core.attempt_start_plan import prepare_start,create_start,finalize_start
from core.economic_source_creation import SourceCreationError
from core.dispatch_observations import bind_generic_observation
from core.registry import registry
from modules.orders.production import OrderFulfillmentService
from modules.organizations.services import EducationPolicyService
from modules.printers import dispatch,dispatch_acquisition
from unittest.mock import patch
registry._providers['OrderFulfillmentProvider']=OrderFulfillmentService();registry._providers['EducationPolicyProvider']=EducationPolicyService()
root=Path(tempfile.mkdtemp(prefix='owned-start-plan-'));payload=root/'owned.gcode';payload.write_text('; inert fixture\nG90\n')
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'owned',TRUE)")
 c.exec_driver_sql("INSERT INTO printers(id,name,is_active,api_type,api_host,org_id,gcode_state) VALUES(59,'owned inert',TRUE,'moonraker','127.0.0.1',1,'IDLE'),(60,'owned legacy',TRUE,'moonraker','127.0.0.2',1,'IDLE')")
 c.exec_driver_sql("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,1,'PLA'),(60,1,'PLA')")
 c.exec_driver_sql("INSERT INTO models(id,name,org_id) VALUES(51,'owned',1)")
 c.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(52,51,'owned.gcode',:path,1)"),{'path':str(payload)})
 c.exec_driver_sql("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id,hold) VALUES(53,51,'owned','scheduled',59,1,FALSE),(54,51,'owned legacy','scheduled',60,1,FALSE)")
names={}
def sender(job,path,name,creds):names[59 if job==53 else 60]=name;return True,'Owned external sender stub'
# Only network transport/notification and external idle telemetry suppressed;
# actual dispatch/acquire/admit/hash/snapshot/claim/receipt runtime runs.
with patch.object(dispatch,'_dispatch_moonraker',sender),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
 assert dispatch.dispatch_job(59,53)[0]
 assert dispatch.dispatch_job(60,54)[0]
key=auth.SECRET_KEY.encode();checks=[]
def prep(c,scope,printer=59,name=None):
 scope.fence_dispatch_membership(c,education=True)
 wire=name or names[printer]
 return prepare_start(c,scope,printer_id=printer,observed_filename=wire,protocol='moonraker',observation_fields={'filename':wire,'job_name':wire,'dispatch_key':key})
def counts():
 with engine.connect() as c:return tuple(c.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings'])
before=counts()
with engine.connect() as observer:before_status=observer.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
with engine.begin() as c:
 c.exec_driver_sql("CREATE FUNCTION owned_plan_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned deferred failure' USING ERRCODE='23514'; END $$")
 c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_plan_fault AFTER INSERT ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_plan_fault()')
try:
 with engine.begin() as c:
  with start_transaction(c) as scope:
   plan=prep(c,scope);assert plan.branch=='fresh_authorized' and plan.job_id==53
   create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(c,scope,plan)
except Exception as error:assert getattr(error.orig,'sqlstate',None)=='23514'
else:raise AssertionError('deferred fault admitted')
assert counts()==before
with engine.connect() as c:
 assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() is None
 assert c.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()==before_status
with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_plan_fault ON print_jobs');c.exec_driver_sql('DROP FUNCTION owned_plan_fault()')
checks.append('real deferred COMMIT23514 rolls back fresh row/lifetime/registry/bindings/job/receipt')
with engine.begin() as c:
 with start_transaction(c) as scope:
  plan=prep(c,scope);record=scope._start_plan_records[id(plan)]
  identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});assert finalize_start(c,scope,plan)==identifier
 assert record=={'finalized':True} and scope._start_plan_records=={}
checks.append('actual durable dispatch private factory/finalize receipt and explicit scope cleanup')
with engine.begin() as c:
 row=c.execute(text('SELECT economic_birth_nonce,economic_uid,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).one();assert row[0] and row[1] is None and row[2]==53
 with start_transaction(c) as scope:
  plan=prep(c,scope);assert plan.branch=='resume' and plan.resume_id==identifier;assert finalize_start(c,scope,plan)==identifier
checks.append('fresh unresolved economic lifetime under exact standalone transport authority resumes no rebirth')
# Genuine retained legacy linkage, no birth/snapshot retrofit.
with engine.begin() as c:
 legacy=c.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,scheduled_job_id,status,started_at,observed_received_at) VALUES(60,:name,:name,54,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING id"),{'name':names[60]}).scalar_one()
assert bind_generic_observation(engine,printer_id=60,print_job_id=legacy,key=key)=='matched'
for printer,branch,attempt_identifier in [(59,'resume',identifier),(60,'legacy_resume',legacy)]:
 with engine.begin() as c:
  with start_transaction(c) as scope:
   plan=prep(c,scope,printer=printer);assert plan.branch==branch;assert finalize_start(c,scope,plan)==attempt_identifier
  with start_transaction(c) as scope:
   plan=prep(c,scope,printer=printer,name='wrong-observation.gcode');assert plan.branch=='fresh_unresolved' and plan.resume_id is None
 checks.append(branch+' exact incoming name required')
with engine.connect() as c:assert c.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':legacy}).scalar_one() is None
# Same-session captured physical tuple drift must abort and outer rollback.
with engine.connect() as c:
 tx=c.begin()
 try:
  with start_transaction(c) as scope:
   plan=prep(c,scope);c.execute(text("UPDATE print_jobs SET job_name='drift' WHERE id=:id"),{'id':identifier});finalize_start(c,scope,plan)
 except SourceCreationError:tx.rollback()
 else:raise AssertionError('captured tuple drift admitted')
checks.append('exact captured resume tuple drift refusal and caller outer rollback')
with engine.connect() as c:
 tx=c.begin()
 try:
  with start_transaction(c) as scope:
   plan=prep(c,scope);record=scope._start_plan_records[id(plan)];raise RuntimeError('owned exit')
 except RuntimeError:pass
 assert record=={'finalized':True} and scope._start_plan_records=={}
 tx.rollback()
checks.append('exception exit retained scope/plan/record clears ephemeral key')
print(json.dumps({'passed':True,'checks':checks,'scope':'actual current registered PostgreSQL generic StartPlan helper; real admission with transport/idle telemetry stub; standalone transport authority, no org/global financial/EDU/callback activation'}),flush=True)
import shutil
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose();print('attempt-start-plan-generic-native-pass',flush=True)
