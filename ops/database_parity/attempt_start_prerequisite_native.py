"""Owned real PostgreSQL queue payload acceptance; never dispatches."""
import os,stat,json,hashlib,tempfile,zipfile
from pathlib import Path
os.environ['JWT_SECRET_KEY']='owned-queue-native-signing-key-only'
private=Path(os.environ['START_NATIVE_PASSWORD_FILE'])
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
from core.economic_source_creation import _execute,SourceCreationError
from core.db_utils import _ConnectionAdapter
from core.order_production import prepare_parent_graph,recheck_parent_graph
from core.registry import registry
from modules.orders.production import OrderFulfillmentService
registry._providers['OrderFulfillmentProvider']=OrderFulfillmentService()
checks=[]
def code(e):return getattr(getattr(e,'orig',e),'sqlstate',None)
for kind in (() if os.environ.get('START_NATIVE_CASES')=='expiry' else ('session','connection','adapter')):
 raw=None
 if kind=='session':ex=Session(engine);ex.begin()
 elif kind=='connection':ex=engine.connect();ex.begin()
 else:raw=engine.raw_connection();ex=_ConnectionAdapter(raw,True);ex.cursor().execute('BEGIN')
 try:
  with start_transaction(ex) as scope:
   scope.fence_dispatch_membership(ex);scope.require_fenced(ex)
   _execute(ex,"INSERT INTO system_config(key,value) VALUES('owned_start_scope','{}')")
  (raw or ex).rollback()
  with engine.connect() as fresh:assert fresh.execute(text("SELECT count(*) FROM system_config WHERE key='owned_start_scope'")).scalar_one()==0
  checks.append(kind+' current scope and complete outer rollback')
 finally:(raw or ex).close()
for terminal in ('commit','rollback'):
 raw=engine.raw_connection();ex=_ConnectionAdapter(raw,True)
 try:
  ex.cursor().execute('BEGIN')
  try:
   with start_transaction(ex) as scope:
    scope.fence_dispatch_membership(ex);getattr(raw,terminal)();ex.cursor().execute('BEGIN');scope.checkpoint(ex)
  except SourceCreationError as error:
   assert str(error)=='Attempt preparation transaction marker expired',str(error)
   checks.append('adapter '+terminal+' reBEGIN exact marker expired refusal')
  else:raise AssertionError('expired marker admitted')
 finally:raw.rollback();raw.close()
if os.environ.get('START_NATIVE_CASES')=='expiry':
 print(json.dumps({'passed':True,'checks':checks,'scope':'affected exact adapter commit/rollback reBEGIN expiry only; direct helper, no HTTP/session staging/StartPlan'}),flush=True)
 engine.dispose();configured_db.engine.dispose();print('attempt-start-prerequisite-native-pass',flush=True);raise SystemExit(0)
for initial,expected in [(0,2000),(9000,2000),(37,37)]:
 with Session(engine) as ex:
  ex.begin();ex.execute(text("SELECT set_config('lock_timeout',:value,true)"),{'value':str(initial)+'ms'})
  with start_transaction(ex) as scope:
   scope.fence_dispatch_membership(ex);assert int(ex.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one())==expected
  ex.rollback()
 checks.append('lock timeout '+str(initial)+' -> '+str(expected))
# Actual INSERT obtains RowExclusive even when SELECT supplies zero rows. No invented claim authority.
for table in ('printer_dispatch_claims','dispatch_attempt_receipts'):
 with engine.connect() as writer:
  writer.begin();writer.exec_driver_sql(f'INSERT INTO {table} SELECT * FROM {table} WHERE FALSE')
  with Session(engine) as ex:
   ex.begin()
   try:
    with start_transaction(ex) as scope:scope.fence_dispatch_membership(ex)
   except Exception as e:assert code(e)=='55P03'
   else:raise AssertionError('writer not fenced')
   ex.rollback()
  writer.rollback()
 with Session(engine) as ex:
  ex.begin()
  with start_transaction(ex) as scope:
   scope.fence_dispatch_membership(ex)
   with engine.connect() as writer:
    writer.begin();writer.exec_driver_sql("SET LOCAL lock_timeout='40ms'")
    try:writer.exec_driver_sql(f'INSERT INTO {table} SELECT * FROM {table} WHERE FALSE')
    except Exception as e:assert code(e)=='55P03'
    else:raise AssertionError('insert not fenced')
    writer.rollback()
  ex.rollback()
 checks.append(table+' real INSERT lock writer-first and preparation-first55P03 (empty insertion, no authority claim)')
with engine.begin() as c:
 c.execute(text("INSERT INTO products(id,name) VALUES(90,'owned')"));c.execute(text("INSERT INTO orders(id,order_number) VALUES(91,'owned')"));c.execute(text('INSERT INTO order_items(id,order_id,product_id) VALUES(92,91,90)'));c.execute(text("INSERT INTO jobs(id,item_name,order_item_id) VALUES(93,'legacy owned graph',92)"))
for kind in ('session','connection','adapter'):
 raw=None
 if kind=='session':ex=Session(engine);ex.begin()
 elif kind=='connection':ex=engine.connect();ex.begin()
 else:raw=engine.raw_connection();ex=_ConnectionAdapter(raw,True);ex.cursor().execute('BEGIN')
 try:
  graph=prepare_parent_graph(ex,[93]);assert graph[0]==(91,) and graph[1]==(92,)
  # Caller must lock job and reprove ancestry before any start mutation.
  _execute(ex,'SELECT id FROM jobs WHERE id=93 FOR UPDATE NOWAIT');assert prepare_parent_graph(ex,[93])==graph
  (raw or ex).rollback();checks.append(kind+' actual parent graph and post-job-lock ancestry reproof')
 finally:(raw or ex).close()
# Existing physical transactions admit SELECT-only recheck; logical-only PG refuses.
for kind in ('session','connection'):
 ex=Session(engine) if kind=='session' else engine.connect()
 try:
  ex.begin()
  try:recheck_parent_graph(ex,((),(),()))
  except SourceCreationError as error:assert str(error)=='Caller physical transaction required'
  else:raise AssertionError('logical-only PG transaction silently admitted')
  ex.execute(text('SELECT 1'));assert recheck_parent_graph(ex,((),(),()))==((),(),())
  ex.rollback();checks.append(kind+' SELECT-only recheck physical positive/no autostart')
 finally:ex.close()
# Actual admissible Education insert fixtures, rolled back in every contention case.
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'owned education',TRUE)")
 c.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'ownedstudent','fixture','viewer',TRUE,1)")
 c.exec_driver_sql("INSERT INTO printers(id,name,model,api_type,org_id) VALUES(9,'inert fixture','fixture','bambu',1)")
 c.exec_driver_sql("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(7,1,'owned','own','Owned','OWN',1)")
 for id in (11,15):
  c.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,expected_hash,reservation_released_at) VALUES(:operation,1,1,'committed',1,1,1,'fixture',CURRENT_TIMESTAMP)"),{'operation':'owned-'+str(id)})
  c.execute(text("INSERT INTO print_files(id,filename,org_id) VALUES(:id,'owned.3mf',1)"),{'id':id})
  c.execute(text("INSERT INTO models(id,name,org_id) VALUES(:id,'owned',1)"),{'id':id})
  c.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(:id,'owned','scheduled',1)"),{'id':id})
 c.exec_driver_sql("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status) VALUES(14,1,'owned-11',11,11,11,7,1,'scheduled')")
inserts={
 'education_cost_centers':"INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(8,1,'new','new','New','NEW',1)",
 'education_cost_center_printers':"INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,granted_by) VALUES(1,7,9,1)",
 'education_submissions':"INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status) VALUES(15,1,'owned-15',15,15,15,7,1,'scheduled')",
 'education_monitor_claims':"INSERT INTO education_monitor_claims(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) VALUES('owned-monitor',1,14,11,9,1,'"+'a'*64+"','reserved')"}
for table,statement in inserts.items():
 with engine.connect() as writer:
  writer.begin();writer.exec_driver_sql(statement)
  with Session(engine) as ex:
   ex.begin()
   try:
    with start_transaction(ex) as scope:scope.fence_dispatch_membership(ex,education=True)
   except Exception as error:assert code(error)=='55P03'
   else:raise AssertionError('Education writer not fenced')
   ex.rollback()
  writer.rollback()
 with Session(engine) as ex:
  ex.begin()
  with start_transaction(ex) as scope:
   scope.fence_dispatch_membership(ex,education=True);scope.require_fenced(ex,education=True)
   with engine.connect() as writer:
    writer.begin();writer.exec_driver_sql("SET LOCAL lock_timeout='40ms'")
    try:writer.exec_driver_sql(statement)
    except Exception as error:assert code(error)=='55P03'
    else:raise AssertionError('Education insert not fenced')
    writer.rollback()
  ex.rollback()
 with engine.connect() as released:
  released.begin();released.exec_driver_sql(statement);released.rollback()
 checks.append(table+' actual admissible INSERT writer-first/preparation-first55P03/release and outer rollback')
for table,id in [('orders',91),('order_items',92)]:
 with engine.connect() as held:
  held.begin();held.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE'),{'id':id})
  with Session(engine) as ex:
   ex.begin()
   try:prepare_parent_graph(ex,[93])
   except Exception as e:assert code(e)=='55P03'
   else:raise AssertionError('parent contention admitted')
   ex.rollback()
  held.rollback()
 checks.append(table+' real row NOWAIT55P03')
print(json.dumps({'passed':True,'checks':checks,'scope':'actual current bootstrap PostgreSQL transaction/parent preparation prerequisite only; no monitor activation/financial authorization'}),flush=True)
engine.dispose();configured_db.engine.dispose();print('attempt-start-prerequisite-native-pass',flush=True)
