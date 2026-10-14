"""Actual PostgreSQL private read HTTP; only physical sender/status/notifications stubbed."""
import os, stat, secrets, json, tempfile, hashlib, shutil, jwt
from sqlalchemy.exc import DBAPIError
from fastapi.responses import JSONResponse
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
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True,connect_args={'options':'-c lock_timeout=200ms'})
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
from modules.orders.routes.attempt_valuation import router as valuation_router
from modules.organizations.routes_auth import router as login_router
from modules.archives import valuation_private_read as read_service
from core.errors import OdinError
passwords={name:secrets.token_urlsafe(32) for name in ('platform','organization','foreign')}
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned organization',TRUE),(2,'Foreign organization',TRUE)")
 for identifier,name,group in ((71,'platform',None),(72,'organization',1),(73,'foreign',2)):
  c.execute(text('INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(:id,:name,:hash,\'admin\',TRUE,:group)'),{'id':identifier,'name':name,'hash':auth.hash_password(passwords[name]),'group':group})
 c.exec_driver_sql("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(80,'Owned','Native','PLA','ffffff')")
app=FastAPI();app.add_middleware(AuditActorMiddleware)
for router in (orders_router,spool_router,valuation_router,login_router):app.include_router(router,prefix='/api')
def database(request:Request):
 with Session(engine,autoflush=False) as db:
  register_request_session(db,request)
  try:yield db
  finally:clear_session(db)
app.dependency_overrides[get_db]=database
diagnostic=os.environ.get('VALUATION_READ_DIAGNOSTIC')=='1'
diagnostic_stage=['request_authority'];diagnostic_errors=[]
if diagnostic:
 for stage in ('verify_financial_reader','fence_content_membership','lock_financial_attempt','prove_financial_graph','assert_content_visible','load_bounded_snapshot','parse_private_valuation','verify_retained_basis'):
  original=getattr(read_service,stage)
  def observed(*args,_stage=stage,_original=original,**kwargs):
   diagnostic_stage[0]=_stage
   return _original(*args,**kwargs)
  setattr(read_service,stage,observed)
@app.exception_handler(DBAPIError)
async def db_error(request,error):
 state=getattr(error.orig,'sqlstate',None)
 if not isinstance(state,str) or len(state)!=5 or not state.isascii() or not state.isalnum():state=None
 if diagnostic:
  value={'stage':diagnostic_stage[0],'exception_class':type(error.orig).__name__,'sqlstate':state,'statement_sha256':hashlib.sha256(' '.join((error.statement or '').split()).encode()).hexdigest()}
  diagnostic_errors.append(value)
  print('valuation-read-diagnostic-'+json.dumps(value,sort_keys=True),flush=True)
 return JSONResponse(status_code=503,content={'sqlstate':state})
@app.exception_handler(OdinError)
async def odin_error(request,error):return JSONResponse(status_code=error.status,content=error.to_envelope())
def bearer(name):return {'Authorization':'Bearer '+auth.create_access_token({'sub':name})}
def key(user,scopes):
 raw='odin_'+secrets.token_urlsafe(32)
 with engine.begin() as c:identifier=c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,'owned-reader',:hash,:prefix,:scopes) RETURNING id"),{'u':user,'hash':auth.hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(scopes)}).scalar_one()
 return {'X-API-Key':raw},identifier
attempts={}
with TestClient(app,raise_server_exceptions=False) as client:
 for offset,(name,group) in enumerate((('platform',None),('organization',1))):
  pid=59+offset;mid=101+offset;fid=58+offset;path=root/(name+'.gcode');path.write_text('; inert owned\nG90\n')
  with engine.begin() as c:
   c.execute(text("INSERT INTO printers(id,name,is_active,api_type,api_host,org_id,gcode_state,bed_x_mm,bed_y_mm,nozzle_diameter) VALUES(:id,:name,TRUE,'moonraker',:host,:org,'IDLE',256,256,.4)"),{'id':pid,'name':name,'org':group,'host':'127.0.0.1' if group is None else '127.0.0.2'})
   c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(:id,1,'PLA')"),{'id':pid})
   c.execute(text("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(:id,:name,:org,1,1,'PLA')"),{'id':mid,'name':name,'org':group})
   c.execute(text('INSERT INTO products(id,name,org_id,price) VALUES(:id,:name,:org,20)'),{'id':mid,'name':name,'org':group})
   c.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(:id,:id,1)'),{'id':mid})
   c.execute(text('INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(:id,:model,:name,:path,:org)'),{'id':fid,'model':mid,'name':name+'.gcode','path':str(path),'org':group})
  headers=bearer(name)
  response=client.post('/api/orders',headers=headers,json={'order_number':'OWNED-'+name,'items':[{'product_id':mid,'quantity':1,'unit_price':20}]});status=response.status_code;assert status==200,('order',name,status)
  order=response.json()['id']
  response=client.post(f'/api/orders/{order}/schedule',headers=headers);status=response.status_code;assert status==200,('schedule',name,status)
  response=client.post('/api/spools',headers=headers,json={'filament_id':80,'purchase_evidence':{'currency':'USD','price':'12.99','net_weight_g':'750.25'}});status=response.status_code;assert status==200,('spool',name,status)
  spool=response.json()['id']
  with engine.begin() as c:
   job=c.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'),{'id':order}).scalar_one()
   c.execute(text('UPDATE jobs SET printer_id=:printer,print_file_id=:file,plate_index=0,payload_sha256=:sha WHERE id=:id'),{'printer':pid,'file':fid,'sha':hashlib.sha256(path.read_bytes()).hexdigest(),'id':job})
   c.execute(text('UPDATE filament_slots SET assigned_spool_id=:spool WHERE printer_id=:printer'),{'spool':spool,'printer':pid})
   c.execute(text('INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES(:scope,:key,\'USD\')'),{'scope':'global' if group is None else 'org','key':0 if group is None else group})
  from core.dispatch_identity import build_identity,ownership_refusal
  loaded=dispatch._load_job(job);creds=dispatch._get_printer_info(pid)
  assert loaded is not None and creds is not None
  assert loaded['printer_id']==pid and loaded['charged_to_org_id']==group and loaded['model_org_id']==group and loaded['print_file_org_id']==group
  assert loaded['plate_index']==0 and loaded['payload_sha256']==hashlib.sha256(path.read_bytes()).hexdigest() and loaded['stored_path']==str(path)
  assert loaded['education_submission_id'] is None and ownership_refusal(loaded,creds) is None
  assert creds['api_type']=='moonraker' and creds['org_id']==group and creds['is_active'] is True
  with engine.connect() as c:
   assert c.execute(text('SELECT gcode_state FROM printers WHERE id=:id'),{'id':pid}).scalar_one()=='IDLE'
   assert c.execute(text('SELECT count(*) FROM education_upload_operations WHERE final_path=:path'),{'path':str(path)}).scalar_one()==0
  identity=build_identity(creds,auth.SECRET_KEY.encode());assert identity is not None
  if group is not None:
   previous=build_identity(dispatch._get_printer_info(59),auth.SECRET_KEY.encode());assert previous is not None and previous.target_key!=identity.target_key
   collision=dict(creds);collision['ip']=dispatch._get_printer_info(59)['ip']
   assert build_identity(collision,auth.SECRET_KEY.encode()).target_key==previous.target_key
   with engine.begin() as c:c.execute(text("UPDATE printers SET api_host='127.0.0.1' WHERE id=:id"),{'id':pid})
   sent=[]
   with patch.object(dispatch,'_dispatch_moonraker',lambda *a:(sent.append(True) or True,'Owned inert sender')),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
    refused=dispatch.dispatch_job(pid,job)
   assert refused[0] is False and sent==[],('same-target-refusal',name)
   with engine.begin() as c:
    assert c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job}).scalar_one() in ('scheduled','pending')
    c.execute(text("UPDATE printers SET api_host='127.0.0.2' WHERE id=:id"),{'id':pid})
   print('valuation-read-native-same-configured-target-refused-no-sender-pass',flush=True)
  print('valuation-read-native-admissible-'+name+'-fixture-independent-target-pass',flush=True)
  names=[]
  def sender(job,path,name,creds):names.append(name);return True,'Owned inert sender'
  with patch.object(dispatch,'_dispatch_moonraker',sender),patch.object(dispatch,'_ws',lambda *a:None),patch.object(dispatch,'_notify_start_outcome',lambda *a:None),patch.object(dispatch_acquisition,'bounded_status_is_idle',lambda *a:True):
   result=dispatch.dispatch_job(pid,job)
   if not result[0]:
    known=('Dispatch ownership could not be verified','Private payload resources require an explicit matching job tenant','Private printer requires an explicit matching job tenant or sharing','Education file requires its approval workflow; no hardware command was sent','Education resource requires its submission and approval workflow; no hardware command was sent')
    classification=result[1] if result[1] in known else 'dispatch_refused_unclassified'
    print(json.dumps({'fixture':name,'dispatch_refusal':classification}),flush=True)
   assert result[0],('dispatch',name)
  with engine.begin() as c:
   with start_transaction(c) as scope:
    scope.fence_dispatch_membership(c,education=True)
    plan=prepare_start(c,scope,printer_id=pid,observed_filename=names[0],protocol='moonraker',observation_fields={'filename':names[0],'job_name':names[0],'dispatch_key':auth.SECRET_KEY.encode()},capture_required=True)
    assert plan.branch=='fresh_authorized'
    identifier=create_start(c,scope,plan,{'started_at':'2026-10-13 12:00:00'})
    assert capture_attempt_spools(c,identifier,pid,source='owned-native',start_scope=scope,start_plan=plan)
    assert finalize_start(c,scope,plan)==identifier
  attempts[name]={'id':identifier,'order':order,'job':job,'spool':spool,'file':fid,'model':mid,'path':str(path)}
 print('valuation-read-native-genuine-org-company-025-order-dispatch-capture-pass',flush=True)
 def read(name,headers):return client.get('/api/orders/attempts/'+str(attempts[name]['id'])+'/unit-valuation',headers=headers)
 def check(response,status,label):
  actual=response.status_code;assert actual==status,(label,actual)
  if status==200:
   value=response.json();assert value['state']=='known' and value['sources'][0]['price_decimal']=='12.99' and value['sources'][0]['net_weight_g_decimal']=='750.25'
   assert response.headers['cache-control']=='private, no-store'
  else:assert '12.99' not in response.text
 # Financial-history hashes exclude intentionally best-effort authentication telemetry.
 def history():
  tables=('print_jobs','spools','spool_purchase_evidence','cost_source_lifetimes','cost_identities','cost_attempt_bindings','cost_job_bindings','cost_global_order_origins','orders','order_items','jobs','finance_currency_settings')
  with engine.connect() as c:
   return {table:hashlib.sha256(json.dumps([list(r) for r in c.execute(text('SELECT * FROM '+table+' ORDER BY 1'))],default=str,sort_keys=True).encode()).hexdigest() for table in tables}
 def owner_negatives(headers):
  before=history();original_parent=read_service.lock_financial_attempt;original_load=read_service.load_bounded_snapshot
  def owner_drift(db,identifier):
   value=original_parent(db,identifier);db.execute(text('UPDATE orders SET org_id=2 WHERE id=:id'),{'id':attempts['organization']['order']});return value
  with patch.object(read_service,'lock_financial_attempt',owner_drift):
   response=read('organization',headers);status=response.status_code
   assert status==503,('immutable-owner-guard',status)
   assert response.json()=={'sqlstate':'23514'},'immutable-owner-guard SQLstate differs'
  assert history()==before
  with engine.connect() as c:assert c.execute(text('SELECT org_id FROM orders WHERE id=:id'),{'id':attempts['organization']['order']}).scalar_one()==1
  def target_drift(db,**kwargs):
   payload=db.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':attempts['organization']['id']}).scalar_one()
   value=json.loads(payload);value['unit_valuation']['target']['owner_key']=2
   db.execute(text('UPDATE print_jobs SET spool_snapshot=:payload WHERE id=:id'),{'payload':json.dumps(value),'id':attempts['organization']['id']})
   return original_load(db,**kwargs)
  with patch.object(read_service,'load_bounded_snapshot',target_drift):check(read('organization',headers),404,'admissible-snapshot-target-mismatch')
  assert history()==before
  print('valuation-read-native-immutable-owner23514-and-admissible-target404-rollback-pass',flush=True)
 if os.environ.get('VALUATION_READ_FOCUSED_NEGATIVE')=='1':
  headers=bearer('organization');check(read('organization',headers),200,'focused-owner-JWT')
  owner_negatives(headers)
  shutil.rmtree(root);engine.dispose();configured_db.engine.dispose()
  print('attempt-valuation-read-focused-negative-pass',flush=True)
  raise SystemExit(0)
 for name,user in (('platform',71),('organization',72)):
  response=read(name,bearer(name))
  if diagnostic:
   status=response.status_code
   assert status==503 and len(diagnostic_errors)==1,('diagnostic-first-read',status)
   assert response.json()=={'sqlstate':diagnostic_errors[0]['sqlstate']}
   shutil.rmtree(root);engine.dispose();configured_db.engine.dispose()
   print('attempt-valuation-read-diagnostic-collected',flush=True)
   raise SystemExit(0)
  check(response,200,name+'-JWT')
  for scopes in (['read'],['read','read:spools']):check(read(name,key(user,scopes)[0]),200,name+'-hashed')
  for scopes in (['read:spools'],['read:jobs'],['write']):check(read(name,key(user,scopes)[0]),403,name+'-narrow')
 check(read('platform',bearer('organization')),404,'foreign-global');check(read('organization',bearer('foreign')),404,'foreign-org')
 print('valuation-read-native-actual-JWT-and-hashed-scopes-org-company-pass',flush=True)
 baseline=history();org_headers=bearer('organization');org_token=jwt.decode(org_headers['Authorization'].partition(' ')[2],auth.SECRET_KEY,algorithms=[auth.ALGORITHM])
 scoped,keyid=key(72,['read','read:spools'])
 # Every actual HTTP request reacquires its own caller-owned request Session.
 writers={
  'user':("UPDATE users SET is_active=FALSE WHERE id=72",{},org_headers),
  'key':("UPDATE api_tokens SET scopes='[\"write\"]' WHERE id=:id",{'id':keyid},scoped),
  'blacklist':("INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,'2099-01-01')",{'jti':org_token['jti']},org_headers),
  'alias_membership':("INSERT INTO print_files(model_id,filename,stored_path,org_id) VALUES(NULL,'owned-contender.gcode',:path,2)",{'path':attempts['organization']['path']},org_headers),
 }
 original_load=read_service.load_bounded_snapshot
 for label,(statement,params,headers) in writers.items():
  with engine.connect() as writer:
   wt=writer.begin();writer.execute(text(statement),params)
   try:
    response=read('organization',headers);status=response.status_code;assert status==503,(label,'writer-first',status)
    assert response.json()=={'sqlstate':'55P03'}
   finally:wt.rollback()
  calls=[]
  def contender(*args,**kwargs):
   with engine.connect() as writer:
    wt=writer.begin()
    try:
     writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
     try:writer.execute(text(statement),params)
     except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='55P03',label;calls.append(True)
     else:raise AssertionError('Reader-first writer admitted: '+label)
    finally:wt.rollback()
   return original_load(*args,**kwargs)
  with patch.object(read_service,'load_bounded_snapshot',contender):check(read('organization',headers),200,label+'-reader-first')
  assert calls==[True]
  with engine.connect() as writer:
   wt=writer.begin()
   try:writer.execute(text(statement),params)
   finally:wt.rollback()
 print('valuation-read-native-user-key-blacklist-alias-bothorders-55P03-release-pass',flush=True)
 from core import financial_read_authority
 original_reader=read_service.verify_financial_reader
 for label,statement in (('current-user',"UPDATE users SET group_id=2 WHERE id=72"),('current-key',"UPDATE api_tokens SET scopes='[\"write\"]' WHERE id="+str(keyid))):
  def changed(db,request,actor):db.execute(text(statement));return original_reader(db,request,actor)
  with patch.object(read_service,'verify_financial_reader',changed):check(read('organization',scoped if label=='current-key' else org_headers),403,label+'-drift')
 owner_negatives(org_headers)
 from sqlalchemy import event
 transfers=[]
 def observe(c,cursor,sql,params,context,many):
  if sql.lstrip().startswith('SELECT spool_snapshot FROM print_jobs'):transfers.append(cursor.rowcount)
 def oversize(db,**kwargs):
  db.execute(text('UPDATE print_jobs SET spool_snapshot=:snapshot WHERE id=:id'),{'snapshot':json.dumps({'version':1,'padding':'é'*32768},ensure_ascii=False),'id':attempts['organization']['id']})
  return original_load(db,**kwargs)
 event.listen(engine,'after_cursor_execute',observe)
 try:
  with patch.object(read_service,'load_bounded_snapshot',oversize):check(read('organization',org_headers),404,'oversized-UTF8')
 finally:event.remove(engine,'after_cursor_execute',observe)
 assert transfers==[0]
 def current_price_edit(db,**kwargs):
  db.execute(text('UPDATE spools SET price=999 WHERE id=:id'),{'id':attempts['organization']['spool']})
  return original_load(db,**kwargs)
 with patch.object(read_service,'load_bounded_snapshot',current_price_edit):check(read('organization',org_headers),200,'retained-no-repricing')
 original_content=read_service.assert_content_visible
 def foreign_alias(db,**kwargs):
  db.execute(text("INSERT INTO print_files(filename,stored_path,org_id) VALUES('owned-alias-negative.gcode',:path,2)"),{'path':attempts['organization']['path']})
  return original_content(db,**kwargs)
 with patch.object(read_service,'assert_content_visible',foreign_alias):check(read('organization',org_headers),404,'foreign-alias')
 assert history()==baseline
 print('valuation-read-native-current-authority-ownerdrift-UTF8-zero-transfer-retained-no-reprice-alias-nohistorymutation-pass',flush=True)
 # Real tracked password login selects cookie ahead of supplied foreign bearer.
 response=client.post('/api/auth/login',data={'username':'organization','password':passwords['organization']});status=response.status_code;assert status==200,('login',status)
 check(read('organization',bearer('foreign')),200,'tracked-cookie-priority')
 cookie=client.cookies.get('session');cookie_payload=jwt.decode(cookie,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])
 statement='DELETE FROM active_sessions WHERE token_jti=:jti';params={'jti':cookie_payload['jti']}
 with engine.connect() as writer:
  wt=writer.begin();writer.execute(text(statement),params)
  try:
   response=read('organization',bearer('foreign'));status=response.status_code;assert status==503,('cookie-writer-first',status);assert response.json()=={'sqlstate':'55P03'}
  finally:wt.rollback()
 calls=[]
 def cookie_contender(*args,**kwargs):
  with engine.connect() as writer:
   wt=writer.begin()
   try:
    writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
    try:writer.execute(text(statement),params)
    except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='55P03';calls.append(True)
    else:raise AssertionError('Cookie read-first writer admitted')
   finally:wt.rollback()
  return original_load(*args,**kwargs)
 with patch.object(read_service,'load_bounded_snapshot',cookie_contender):check(read('organization',bearer('foreign')),200,'cookie-reader-first')
 assert calls==[True]
 with engine.connect() as writer:
  wt=writer.begin()
  try:writer.execute(text(statement),params)
  finally:wt.rollback()
 def cookie_removed(db,request,actor):db.execute(text(statement),params);return original_reader(db,request,actor)
 with patch.object(read_service,'verify_financial_reader',cookie_removed):check(read('organization',bearer('platform')),403,'cookie-no-bearer-fallback')
 assert history()==baseline
 print('valuation-read-native-tracked-cookie-priority-revocation-bothorders-release-pass',flush=True)
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose()
print('attempt-valuation-read-native-pass',flush=True)
