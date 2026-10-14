"""Owned PostgreSQL valuation capture; physical sender, idle telemetry and notifications stubbed."""
import os, stat, secrets, json, tempfile, hashlib, shutil
from pathlib import Path
private=Path(os.environ['VALUATION_NATIVE_PASSWORD_FILE'])
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
root=Path(tempfile.mkdtemp(prefix='owned-valuation-'));payload=root/'owned.gcode';payload.write_text('; inert\nG90\n')
with engine.begin() as c:
 c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(71,'platform',:hash,'admin',TRUE)"),{'hash':auth.hash_password(secrets.token_urlsafe(32))})
 c.exec_driver_sql("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(80,'Owned','Native','PLA','ffffff')")
 c.exec_driver_sql("INSERT INTO printers(id,name,is_active,api_type,api_host,org_id,gcode_state,bed_x_mm,bed_y_mm,nozzle_diameter) VALUES(59,'Owned valuation',TRUE,'moonraker','127.0.0.1',NULL,'IDLE',256,256,.4)")
 c.exec_driver_sql("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,1,'PLA')")
 c.exec_driver_sql("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(101,'Owned model',NULL,1,1,'PLA')")
 c.exec_driver_sql("INSERT INTO products(id,name,org_id,price) VALUES(101,'Owned product',NULL,20)")
 c.exec_driver_sql('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,101,1)')
 c.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(58,101,'owned.gcode',:path,NULL)"),{'path':str(payload)})
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(orders_router,prefix='/api');app.include_router(spool_router,prefix='/api')
def database(request:Request):
 with Session(engine,autoflush=False) as db:
  register_request_session(db,request)
  try:yield db
  finally:clear_session(db)
app.dependency_overrides[get_db]=database
headers={'Authorization':'Bearer '+auth.create_access_token({'sub':'platform'})}
with TestClient(app,raise_server_exceptions=False) as client:
 response=client.post('/api/orders',headers=headers,json={'order_number':'OWNED-VALUATION','items':[{'product_id':101,'quantity':1,'unit_price':20}]})
 assert response.status_code==200,('order',response.status_code)
 order_id=response.json()['id']
 response=client.post(f'/api/orders/{order_id}/schedule',headers=headers);assert response.status_code==200,('schedule',response.status_code)
 response=client.post('/api/spools',headers=headers,json={'filament_id':80,'purchase_evidence':{'currency':'USD','price':'0','net_weight_g':'750.25'}})
 assert response.status_code==200,('spool',response.status_code)
 spool_id=response.json()['id']
with engine.begin() as c:
 job_id=c.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'),{'id':order_id}).scalar_one()
 c.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,plate_index=0,payload_sha256=:sha WHERE id=:id'),{'id':job_id,'sha':hashlib.sha256(payload.read_bytes()).hexdigest()})
 c.execute(text('UPDATE filament_slots SET assigned_spool_id=:id WHERE printer_id=59'),{'id':spool_id})
 c.exec_driver_sql("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('global',0,'USD')")
names=[]
def sender(job,path,name,creds):names.append(name);return True,'Owned inert sender'
with patch.object(dispatch,'_dispatch_moonraker',sender),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
 result=dispatch.dispatch_job(59,job_id);assert result[0],('dispatch refused',result[1])
def capture(c):
 with start_transaction(c) as scope:
  scope.fence_dispatch_membership(c,education=True)
  plan=prepare_start(c,scope,printer_id=59,observed_filename=names[0],protocol='moonraker',observation_fields={'filename':names[0],'job_name':names[0],'dispatch_key':auth.SECRET_KEY.encode()},capture_required=True)
  assert plan.branch=='fresh_authorized'
  identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 12:00:00'})
  assert capture_attempt_spools(c,identifier,59,source='owned-native',start_scope=scope,start_plan=plan)
  assert finalize_start(c,scope,plan)==identifier
 return identifier
def counts():
 with engine.connect() as c:return tuple(c.execute(text('SELECT count(*) FROM '+name)).scalar_one() for name in ('print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings'))
before=counts()
with engine.begin() as c:
 c.exec_driver_sql("CREATE FUNCTION owned_value_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned deferred' USING ERRCODE='23514'; END $$")
 c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_value_fault AFTER INSERT ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_value_fault()')
try:
 with engine.begin() as c:capture(c)
except Exception as error:assert getattr(getattr(error,'orig',None),'sqlstate',None)=='23514',type(error).__name__
else:raise AssertionError('Deferred commit fault admitted')
assert counts()==before
with engine.connect() as c:assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':job_id}).scalar_one() is None
with engine.begin() as c:
 c.exec_driver_sql('DROP TRIGGER owned_value_fault ON print_jobs');c.exec_driver_sql('DROP FUNCTION owned_value_fault()')
# Admissible writes must be blocked by the real predicate fences in both orders.
# Every writer transaction rolls back; no fixture row or config edit is retained.
writers = {
 'filament_slots': "INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,2,'PLA')",
 'spools': "INSERT INTO spools(filament_id,org_id,status) VALUES(80,NULL,'active')",
 'print_files': "INSERT INTO print_files(model_id,filename,stored_path,org_id) VALUES(101,'other.gcode','/owned/inert',NULL)",
 'finance_currency_settings': "UPDATE finance_currency_settings SET currency='EUR' WHERE owner_scope='global' AND owner_key=0",
 'missing_finance_config': "INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('global',0,'EUR')",
}
for table, statement in writers.items():
 if table=='missing_finance_config':
  with engine.begin() as setup:setup.exec_driver_sql("DELETE FROM finance_currency_settings WHERE owner_scope='global' AND owner_key=0")
 with engine.connect() as writer:
  tx=writer.begin();writer.exec_driver_sql(statement)
  try:
   try:
    with engine.begin() as c:capture(c)
   except Exception as error:assert getattr(getattr(error,'orig',None),'sqlstate',None)=='55P03',(table,type(error).__name__)
   else:raise AssertionError('Writer-first predicate was admitted: '+table)
  finally:tx.rollback()
 assert counts()==before
 # Successful capture retains every source/config fence until the outer commit.
 with engine.connect() as c:
  tx=c.begin()
  try:
   capture(c)
   with engine.connect() as writer:
    wt=writer.begin()
    try:
     writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
     try:writer.exec_driver_sql(statement)
     except Exception as error:assert getattr(getattr(error,'orig',None),'sqlstate',None)=='55P03',(table,type(error).__name__)
     else:raise AssertionError('Capture-first predicate was admitted: '+table)
    finally:wt.rollback()
  finally:tx.rollback()
 assert counts()==before
 # Exact same admissible writer succeeds after the capture transaction releases.
 with engine.connect() as writer:
  wt=writer.begin()
  try:writer.exec_driver_sql(statement)
  finally:wt.rollback()
 if table=='missing_finance_config':
  with engine.begin() as setup:setup.exec_driver_sql("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('global',0,'USD')")
print('valuation-five-predicate-writers-both-orders-and-release-pass',flush=True)
with engine.begin() as c:identifier=capture(c)
with engine.connect() as c:
 snapshot=json.loads(c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one());value=snapshot['unit_valuation']
 assert snapshot['version']==1 and value['version']==2 and value['state']=='known'
 assert value['captured_at'].endswith('Z') and value['target']['owner_scope']=='global'
 assert value['sources'][0]['price_decimal']=='0' and value['sources'][0]['net_weight_g_decimal']=='750.25'
print(json.dumps({'passed':True,'checks':['real JWT company origin/order/schedule/025 source','actual durable dispatch with physical sender stub','known global private captured exact zero/fractional denominator and UTC','real deferred COMMIT23514 restores known attempt/lifetime/registry/bindings/receipt'],'scope':'Company PostgreSQL private capture plus five admissible predicate writers including absent config both orders/release; no org/callback/projection/COGS certification'}),flush=True)
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose();print('first-start-valuation-native-pass',flush=True)
