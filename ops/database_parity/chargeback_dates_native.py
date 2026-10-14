"""Owned actual PostgreSQL calendar boundaries through real report authentication."""
import os,stat,secrets,tempfile,logging,json
from pathlib import Path
private=Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
owned=tempfile.TemporaryDirectory(prefix='owned-chargeback-');os.environ['ODIN_DATA_DIR']=owned.name
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres';os.environ['DATABASE_PASSWORD_FILE']=str(private)
logging.getLogger('httpx').disabled=True;logging.getLogger('httpcore').disabled=True
from sqlalchemy import create_engine,text,event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True,connect_args={'options':'-c timezone=America/New_York'})
from core.schema import bootstrap_database
bootstrap_database(engine,Path('/source/backend'))
from core import auth
from core.config import settings
settings.api_key=None
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from core.db import get_db
from core.audit_actor import register_request_session,clear_session
from core.middleware.audit_actor import AuditActorMiddleware
from modules.reporting.routes import reports
from modules.organizations.routes_auth import router as login_router
import core.db as configured_db
configured_db.SessionLocal=lambda:Session(engine)
password=secrets.token_urlsafe(32);key='odin_'+secrets.token_urlsafe(32)
with engine.begin() as c:
 assert c.exec_driver_sql('SHOW TimeZone').scalar_one()=='America/New_York'
 assert c.exec_driver_sql("SELECT data_type FROM information_schema.columns WHERE table_name='jobs' AND column_name='created_at'").scalar_one()=='timestamp without time zone'
 c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'owned date org',TRUE),(2,'foreign date org',TRUE)"))
 c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(71,'owned-dates',:hash,'admin',TRUE,1)"),{'hash':auth.hash_password(password)})
 c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(71,'owned',:hash,:prefix,:scopes)"),{'hash':auth.hash_password(key),'prefix':key[:10],'scopes':json.dumps(['read:analytics'])})
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(reports.router);app.include_router(login_router)
def database(request:Request):
 with Session(engine,autoflush=False) as db:
  assert db.execute(text('SHOW TimeZone')).scalar_one()=='America/New_York'
  register_request_session(db,request)
  try:yield db
  finally:clear_session(db)
app.dependency_overrides[get_db]=database
captured=[]
@event.listens_for(engine,'before_cursor_execute')
def observe(conn,cursor,statement,parameters,context,many):
 if 'GROUP BY j.charged_to_user_id' in statement:
  for value in parameters.values():
   if hasattr(value,'tzinfo'):assert value.tzinfo is None;captured.append(value)
checks=[]
with TestClient(app,client=('127.0.0.1',50001)) as client:
 jwt={'Authorization':'Bearer '+auth.create_access_token({'sub':'owned-dates'})}
 for day,nextday in [('2026-10-12','2026-10-13'),('2026-03-08','2026-03-09'),('2026-11-01','2026-11-02')]:
  with engine.begin() as c:
   c.execute(text('DELETE FROM jobs'))
   for timestamp,org,user in [(day+' 00:00:00',1,71),(day+' 23:59:59.999999',1,71),(nextday+' 00:00:00',1,71),(day+' 12:00:00',2,71),(day+' 12:00:00',None,71),(day+' 12:00:00',1,None)]:
    c.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,created_at) VALUES('owned date',:user,:org,CAST(:time AS TIMESTAMP))"),{'user':user,'org':org,'time':timestamp})
  for headers in (jwt,{'X-API-Key':key}):
   result=client.get('/reports/chargebacks',headers=headers,params={'start_date':day,'end_date':day})
   assert result.status_code==200 and len(result.json())==1 and result.json()[0]['job_count']==2
  checks.append('UTC full day '+day+' JWT/key exactorg/null exclusions')
 for params in [{'start_date':'2026-02-30'},{'end_date':'２０２６-10-12'},{'end_date':'20261012'},{'start_date':'2026-10-13','end_date':'2026-10-12'}]:
  assert client.get('/reports/chargebacks',headers=jwt,params=params).status_code==422
 checks.append('invalid/nonASCII/reversed422')
 with engine.begin() as c:
  c.execute(text('DELETE FROM jobs'))
  for stamp in ['9999-12-31 00:00:00','9999-12-31 23:59:59.999999','10000-01-01 00:00:00','infinity']:
   c.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,created_at) VALUES('owned max',71,1,CAST(:time AS TIMESTAMP))"),{'time':stamp})
 result=client.get('/reports/chargebacks',headers=jwt,params={'start_date':'9999-12-31','end_date':'9999-12-31'})
 assert result.status_code==200 and result.json()[0]['job_count']==2
 checks.append('date.max finiteinclusive excludes actualPG year10000/infinity')
 login=client.post('/auth/login',data={'username':'owned-dates','password':password})
 assert login.status_code==200 and bool(client.cookies.get('session'))
 result=client.get('/reports/chargebacks',params={'start_date':'9999-12-31','end_date':'9999-12-31'})
 assert result.status_code==200 and result.json()[0]['job_count']==2
 checks.append('realpasswordlogin trackedcookie positive')
assert captured and all(x.tzinfo is None for x in captured)
engine.dispose();owned.cleanup()
print(json.dumps({'checks':checks,'naive_bound_observations':len(captured),'actual_timezone':'America/New_York'}))
print('chargeback-dates-native-pass')
