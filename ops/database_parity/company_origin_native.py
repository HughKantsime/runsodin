"""Owned real PostgreSQL company-origin acceptance; never dispatches."""
import os,stat,json,hashlib,tempfile,zipfile
from pathlib import Path
os.environ['JWT_SECRET_KEY']='owned-queue-native-signing-key-only'
private=Path(os.environ['ORIGIN_NATIVE_PASSWORD_FILE'])
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
from core.audit_actor import register_request_session,clear_session
from core.middleware.audit_actor import AuditActorMiddleware
from modules.orders.routes import orders_crud
from modules.organizations import routes_auth
from modules.orders.models import Order
from modules.orders.schemas import OrderCreate
from core import global_order_creation
password='OwnedSyntheticLogin123!';os.environ['API_KEY']='owned-legacy-service-key-only'
with engine.begin() as c:
 c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'owned org',TRUE)"))
 for id,name,role,org in [(71,'platform','admin',None),(70,'unassigned','operator',None),(3,'orgadmin','admin',1)]:
  c.execute(text('INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(:id,:name,:hash,:role,TRUE,:org)'),{'id':id,'name':name,'hash':auth.hash_password(password),'role':role,'org':org})
 c.execute(text("INSERT INTO products(id,name,org_id) VALUES(90,'owned global product',NULL)"))
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(orders_crud.router,prefix='/api');app.include_router(routes_auth.router,prefix='/api')
def database(request:__import__('fastapi').Request):
 with Session(engine,autoflush=False) as db:
  db.execute(text("SET SESSION lock_timeout='200ms'"))
  register_request_session(db,request)
  try:yield db
  finally:clear_session(db)
app.dependency_overrides[get_db]=database
checks=[]
def header(name='platform'):return {'Authorization':'Bearer '+auth.create_access_token({'sub':name})}
def counts():
 with engine.connect() as c:return tuple(c.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in ['orders','order_items','cost_identities','cost_global_order_origins'])
def post(h=None,body=None,expected=200,client=None):
 if client is None:
  with TestClient(app,raise_server_exceptions=False) as own:r=own.post('/api/orders',headers=h or header(),json=body or {'order_number':'owned company'})
 else:r=client.post('/api/orders',headers=h or header(),json=body or {'order_number':'owned company'})
 assert r.status_code==expected,('status',expected,r.status_code)
 return r

def source(id):
 with engine.connect() as c:return dict(c.execute(text('SELECT * FROM orders WHERE id=:id'),{'id':id}).mappings().one())
def origin(id):
 row=source(id);assert row['economic_uid'] and len(row['economic_global_origin_nonce'])==32 and row['org_id'] is None
 with engine.connect() as c:
  assert tuple(c.execute(text('SELECT origin_nonce,proof_kind,owner_scope,owner_key FROM cost_global_order_origins WHERE order_uid=:uid'),{'uid':row['economic_uid']}).one())==(row['economic_global_origin_nonce'],'verified_platform_admin_global_creation','global',0)
  assert tuple(c.execute(text('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid'),{'uid':row['economic_uid']}).one())==('order',id,'global',0)
 return row
origin(post().json()['id']);checks.append('actual signed platform JWT fresh nonce/global typed UID/receipt')
for name in ['orgadmin','unassigned']:
 row=source(post(header(name)).json()['id']);assert row['economic_global_origin_nonce'] is None;checks.append(name+' no origin')
row=source(post({'X-API-Key':os.environ['API_KEY']}).json()['id']);assert row['economic_global_origin_nonce'] is None;checks.append('legacy service key no origin')
keys={}
for scopes,expected in [('write',200),('read',403)]:
 raw='odin_owned_origin_'+scopes;keys[scopes]=raw
 with engine.begin() as c:identifier=c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(71,'owned',:hash,:prefix,:scopes) RETURNING id"),{'hash':auth.hash_password(raw),'prefix':raw[:10],'scopes':json.dumps([scopes])}).scalar_one()
 if scopes=='write':key_id=identifier
 before=counts();r=post({'X-API-Key':raw},expected=expected)
 if expected==200:origin(r.json()['id'])
 else:assert counts()==before
 checks.append('actual hashed per-user '+scopes+' scope '+str(expected))
with TestClient(app,raise_server_exceptions=False) as client:
 r=client.post('/api/auth/login',data={'username':'platform','password':password});assert r.status_code==200
 with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM active_sessions WHERE user_id=71')).scalar_one()==1
 origin(post(header('unassigned'),client=client).json()['id']);checks.append('real password login tracked cookie outranks unassigned Bearer')
with TestClient(app,raise_server_exceptions=False) as client:
 client.cookies.set('session',auth.create_access_token({'sub':'platform'}));before=counts();post(client=client,expected=403);assert counts()==before;checks.append('untracked chosen cookie no Bearer fallback')
# Defaults compare original mapped insert against isolated Core clone, whole native raw row.
for extras in [{},{'revenue':0,'labor_minutes':0},{'revenue':12.75,'platform_fees':0.25,'order_date':'2026-10-13T11:30:00+02:00'}]:
 data=OrderCreate.model_validate({'order_number':'owned compare',**extras});fields=('order_number','platform','customer_name','customer_email','order_date','notes','revenue','platform_fees','payment_fees','shipping_charged','shipping_cost')
 with Session(engine) as db:
  reference=Order(**{name:getattr(data,name) for name in fields},labor_minutes=data.labor_minutes or 0,org_id=None);db.add(reference);db.commit();id=reference.id
 expected=source(id);actual=origin(post(body={'order_number':'owned compare',**extras}).json()['id'])
 for row in [expected,actual]:
  for key in ['id','economic_uid','economic_global_origin_nonce']:row.pop(key)
  assert row.pop('created_at') is not None and row.pop('updated_at') is not None
 assert actual==expected
assert 'economic_global_origin_nonce' not in Order.__table__.c
checks.append('native full mapped/Core clone default/date/zero parity; mapper unmodified')
# Writer-first real row/predicate locks. Expected500 is honest route current error contract.
for table,h,statement in [('users',header(),'SELECT id FROM users WHERE id=71 FOR UPDATE'),('api_tokens',{'X-API-Key':keys['write']},'SELECT id FROM api_tokens WHERE id='+str(key_id)+' FOR UPDATE'),('token_blacklist',header(),"INSERT INTO token_blacklist(jti,expires_at) VALUES('owned_uncommitted','2099-01-01')")]:
 before=counts()
 with engine.connect() as held:
  held.begin();held.exec_driver_sql(statement);post(h,expected=500);assert counts()==before;held.rollback()
 checks.append(table+' writer-first actual contention refusal/no graph')
# Creator-first after _locked_actor acquisition; competitor can neither update nor insert.
original=global_order_creation._locked_actor
for table,h,statement in [('users',header(),"UPDATE users SET role=role WHERE id=71"),('api_tokens',{'X-API-Key':keys['write']},'UPDATE api_tokens SET name=name WHERE id='+str(key_id)),('token_blacklist',header(),"INSERT INTO token_blacklist(jti,expires_at) VALUES('owned_competing','2099-01-01')")]:
 seen=[]
 def locked(db,request,actor):
  original(db,request,actor)
  with engine.connect() as competing:
   competing.begin();competing.exec_driver_sql("SET LOCAL lock_timeout='40ms'")
   try:competing.exec_driver_sql(statement)
   except Exception as error:assert getattr(error.orig,'sqlstate',None)=='55P03';seen.append(True)
   else:raise AssertionError('creator fence did not block writer')
   competing.rollback()
 global_order_creation._locked_actor=locked
 try:origin(post(h).json()['id'])
 finally:global_order_creation._locked_actor=original
 assert seen==[True];checks.append(table+' creator-first actual55P03 then success')
before=counts()
with engine.begin() as c:
 c.exec_driver_sql("CREATE FUNCTION owned_origin_commit_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned deferred fault' USING ERRCODE='23514'; END $$")
 c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_origin_commit_fault AFTER INSERT ON orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_origin_commit_fault()')
post(body={'order_number':'owned rollback','items':[{'product_id':90,'quantity':2}]},expected=500);assert counts()==before
with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_origin_commit_fault ON orders');c.exec_driver_sql('DROP FUNCTION owned_origin_commit_fault()')
checks.append('real deferred COMMIT fault whole order inline items registry origin rollback')
print(json.dumps({'passed':True,'checks':checks,'scope':'actual current bootstrap PostgreSQL verified company-origin request creator only; no financial valuation/account-lifetime certification'}),flush=True)
engine.dispose();configured_db.engine.dispose();print('company-origin-native-pass',flush=True)
