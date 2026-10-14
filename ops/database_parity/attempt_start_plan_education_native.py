"""Owned real PostgreSQL company-origin acceptance; never dispatches."""
import os,stat,json,hashlib,tempfile,zipfile
from pathlib import Path
os.environ['JWT_SECRET_KEY']='owned-queue-native-signing-key-only'
private=Path(os.environ['EDU_PLAN_NATIVE_PASSWORD_FILE'])
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
root=Path(tempfile.mkdtemp(prefix='owned-edu-plan-'));payload=root/'owned.3mf'
with zipfile.ZipFile(payload,'w') as z:z.writestr('Metadata/plate_1.gcode','; owned inert first plate\nG90\n')
digest=hashlib.sha256(payload.read_bytes()).hexdigest()
def fact(value,key):return {'present':True,'recognized':True,'source_member':'Metadata/project_settings.config','source_key':key,'value':value}
facts={'api_types':fact(['bambu'],'printer_model'),'machine':fact('P1S','printer_model'),'bed':fact({'x_mm':256,'y_mm':256},'bed_shape'),'nozzle':fact(.4,'nozzle_diameter'),'materials':fact(['PLA'],'filament_type')}
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'owned',TRUE)")
 c.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'student','fixture','viewer',TRUE,1),(3,'teacher','fixture','admin',TRUE,1)")
 c.exec_driver_sql("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(7,1,'owned','owned','Owned','OWN',3)")
 for printer in (9,10,11):
  c.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,api_host,api_key,org_id,shared,bed_x_mm,bed_y_mm,nozzle_diameter,gcode_state) VALUES(:id,:name,'P1S','P1S',TRUE,'bambu','127.0.0.1',:key,1,FALSE,256,256,.4,'IDLE')"),{'id':printer,'name':'owned-'+str(printer),'key':crypto.encrypt('owned-serial-'+str(printer)+'|owned-access-code')})
  c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(:id,1,'PLA')"),{'id':printer})
  c.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,granted_by) VALUES(1,7,:id,3)"),{'id':printer})
  c.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,expected_hash,reservation_released_at) VALUES(:op,1,1,'committed',:size,:size,:size,:digest,CURRENT_TIMESTAMP)"),{'op':'owned-'+str(printer),'size':payload.stat().st_size,'digest':digest})
  c.execute(text("INSERT INTO models(id,name,org_id,category) VALUES(:id,'owned',1,'Education Submission')"),{'id':200+printer})
  c.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,created_by,file_hash,blob_state,storage_bytes,compatibility_facts_json) VALUES(:id,:model,'owned.gcode.3mf','owned.gcode.3mf',:path,1,1,:digest,'present',:size,:facts)"),{'id':100+printer,'model':200+printer,'path':str(payload),'digest':digest,'size':payload.stat().st_size,'facts':json.dumps(facts)})
  c.execute(text('UPDATE models SET print_file_id=:file WHERE id=:model'),{'file':100+printer,'model':200+printer})
  c.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id,submitted_by,approved_by,hold,print_file_id,payload_sha256,plate_index) VALUES(:id,:model,'owned','scheduled',:printer,1,1,3,FALSE,:file,:digest,0)"),{'id':300+printer,'model':200+printer,'printer':printer,'file':100+printer,'digest':digest})
  c.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,approved_by,approved_printer_id,status,lifecycle_revision) VALUES(:id,1,:op,:job,:file,:model,7,1,3,:printer,'scheduled',3)"),{'id':400+printer,'op':'owned-'+str(printer),'job':300+printer,'file':100+printer,'model':200+printer,'printer':printer})
names={}
def sender(job,path,name,creds,**options):names[job-300]=name;return True,'Owned physical sender stub'
with patch.object(dispatch,'_dispatch_bambu',sender),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
 with patch.object(dispatch,'_confirm_education_dispatch',lambda *a:{'transitioned':False}):assert dispatch.dispatch_job(9,309)[0] is False
 for printer in (10,11):assert dispatch.dispatch_job(printer,300+printer)[0]
key=auth.SECRET_KEY.encode();checks=[]
def prepare(c,scope,printer,name=None,filename='Metadata/plate_1.gcode'):
 scope.fence_dispatch_membership(c,education=True)
 wire=name or names[printer]
 return prepare_start(c,scope,printer_id=printer,observed_filename=wire,protocol='bambu',observation_fields={'filename':filename,'job_name':wire,'dispatch_key':key})
def state(printer):
 with engine.connect() as c:
  return {'submission':tuple(c.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=:id'),{'id':400+printer}).one()),'job':c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':300+printer}).scalar_one(),'claim':tuple(c.execute(text('SELECT state,authority_revision FROM education_monitor_claims WHERE job_id=:id'),{'id':300+printer}).one()),'receipt':c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':300+printer}).scalar_one(),'counts':tuple(c.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in ['print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings','education_audit_events'])}
# Both immediate audit error and actual deferred COMMIT failure must restore full observed baseline.
for table,mode in [('education_audit_events','audit'),('print_jobs','deferred')]:
 before=state(9)
 with engine.begin() as c:
  c.exec_driver_sql("CREATE FUNCTION owned_edu_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned failure' USING ERRCODE='23514'; END $$")
  if mode=='audit':c.exec_driver_sql('CREATE TRIGGER owned_edu_fault BEFORE INSERT ON education_audit_events FOR EACH ROW EXECUTE FUNCTION owned_edu_fault()')
  else:c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_edu_fault AFTER INSERT ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_edu_fault()')
 try:
  with engine.begin() as c:
   with start_transaction(c) as scope:
    plan=prepare(c,scope,9);assert plan.branch=='fresh_authorized'
    create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});finalize_start(c,scope,plan)
 except Exception as error:assert getattr(error.orig,'sqlstate',None)=='23514'
 else:raise AssertionError('fault accepted')
 assert state(9)==before
 with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_edu_fault ON '+table);c.exec_driver_sql('DROP FUNCTION owned_edu_fault()')
 checks.append('actual '+mode+'23514 whole outer birth/transition/receipt/audit rollback fresh observer')
ids={}
for printer in (9,10):
 before=state(printer)
 with engine.begin() as c:
  with start_transaction(c) as scope:
   plan=prepare(c,scope,printer);assert plan.branch=='fresh_authorized' and plan.job_id==300+printer
   identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 00:00:00'});assert finalize_start(c,scope,plan)==identifier;ids[printer]=identifier
  assert c.execute(text('SELECT economic_uid,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==(None,300+printer)
 after=state(printer);assert after['submission'][0]=='printing' and after['claim'][0]=='running' and after['receipt']==identifier
 assert after['submission'][1]==before['submission'][1]+(1 if before['claim'][0]=='reserved' else 0)
 with engine.begin() as c:
  with start_transaction(c) as scope:
   plan=prepare(c,scope,printer);assert plan.branch=='resume';assert finalize_start(c,scope,plan)==identifier
 checks.append('actual '+before['claim'][0]+' preinsert/factory unresolved standalone/canonical transition/universal receipt/running resume')
# Canonical legacy transition creates no new lifetime; snapshot bytes preserved across replay.
with engine.begin() as c:
 legacy=c.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at,observed_received_at,spool_snapshot) VALUES(11,'Metadata/plate_1.gcode',:name,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,:snapshot) RETURNING id"),{'name':names[11],'snapshot':'{"version":1,"printer_id":11,"material_plan":[]}'}).scalar_one()
 result=claim_monitor_observation(c,print_job_id=legacy,printer_id=11,observed_filename=names[11]);assert result['authorized'] and result['dispatch_receipt_binding']=='matched'
for printer in (9,11):
 with engine.begin() as c:
  before=tuple(c.execute(text('SELECT economic_birth_nonce,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':ids.get(printer,legacy)}).one())
  with start_transaction(c) as scope:
   plan=prepare(c,scope,printer);assert plan.branch==('legacy_resume' if printer==11 else 'resume');finalize_start(c,scope,plan)
   with_error=prepare(c,scope,printer,filename='odin-'+('f'*32)+'.3mf');assert with_error.branch=='fresh_unresolved' and with_error.resume_id is None
  assert tuple(c.execute(text('SELECT economic_birth_nonce,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':ids.get(printer,legacy)}).one())==before
 checks.append('normal/legacy exact replay preserves birth/snapshot; contradictory token refuses')
# Current entitlement and revision drift are caught before creating a duplicate attempt.
for statement in ["UPDATE education_cost_centers SET state='archived' WHERE id=7", "UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9", "UPDATE education_monitor_claims SET authority_revision=authority_revision+1 WHERE printer_id=9"]:
 before=state(9)
 with engine.connect() as c:
  tx=c.begin();c.exec_driver_sql(statement)
  with start_transaction(c) as scope:assert prepare(c,scope,9).branch=='fresh_unresolved'
  tx.rollback()
 assert state(9)==before
checks.append('current center/grant/revision negatives preserve all prior state after rollback')
print(json.dumps({'passed':True,'checks':checks,'scope':'actual PostgreSQL current EDU preinsert plan authority/factory/canonical transitions/receipt/resume/rollback only; standalone unresolved no prices/financial owner/callback activation'}),flush=True)
import shutil
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose();print('attempt-start-plan-education-native-pass',flush=True)
