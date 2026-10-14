"""Owned current026 PostgreSQL HTTP correction proof; no printer operations."""
import os,stat,json
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
os.environ['JWT_SECRET_KEY']='owned-correction-native-signing-key-only'
private=Path(os.environ['CORRECTION_NATIVE_PASSWORD_FILE'])
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(private)
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
from core.schema import bootstrap_database
bootstrap_database(engine,Path('/source/backend'))
from core import auth,db as configured_db
from core.db import get_db
from core.db_compat import sql
assert configured_db.IS_POSTGRES and sql.is_postgres
with engine.connect() as c,configured_db.engine.connect() as other:
 assert c.execute(text('SELECT current_database()')).scalar_one()==other.execute(text('SELECT current_database()')).scalar_one()=='postgres'
from modules.jobs.routes import jobs_crud
from modules.jobs.models import Job
from core.base import JobStatus
from core.economic_identity import _add_created
from core.registry import registry
from modules.organizations.services import EducationPolicyService
from core.dispatch_claims import acquire
registry._providers['EducationPolicyProvider']=EducationPolicyService()
auth.SECRET_KEY='owned-correction-native-signing-key-only'
with engine.begin() as c:
 c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'owned-school',TRUE),(2,'owned-other',TRUE)"))
 c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'student','fixture','viewer',TRUE,1),(4,'outsider','fixture','viewer',TRUE,1)"))
 c.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,org_id) VALUES(9,'owned inert printer','fixture',TRUE,'bambu',1)"))
app=FastAPI();app.include_router(jobs_crud.router)
def database():
 with Session(engine) as db:
  yield db
app.dependency_overrides[get_db]=database
headers={'Authorization':'Bearer '+auth.create_access_token({'sub':'student','role':'viewer'})}
checks=[]
def seed(owner=1,org=1,status=JobStatus.REJECTED):
 with Session(engine) as db:
  job=Job(item_name='Original',quantity=1,submitted_by=owner,charged_to_org_id=org,status=status,printer_id=9)
  _add_created(db,job,'job',owner_org_id=org);db.commit();identifier=job.id
 with engine.connect() as c:assert c.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':identifier}).scalar_one()
 return identifier
def patch(identifier,body,expected,h=None):
 with TestClient(app,raise_server_exceptions=False) as client:r=client.patch('/jobs/'+str(identifier),json=body,headers=h or headers)
 assert r.status_code==expected,('http',expected,r.status_code)
 return r
def observe(identifier):
 with engine.connect() as c:return dict(c.execute(text('SELECT item_name,quantity,notes,due_date,status FROM jobs WHERE id=:id'),{'id':identifier}).mappings().one())
job=seed();patch(job,{'item_name':'Corrected','quantity':3,'notes':'Owned correction','due_date':'2026-12-01T10:30:00'},200)
fact=observe(job);assert fact['status']=='rejected' and fact['item_name']=='Corrected' and fact['quantity']==3 and fact['notes']=='Owned correction' and fact['due_date'].year==2026
checks.append('four field own real JWT correction remains rejected fresh observer')
for owner,org,body,expected,label in [(4,1,{'notes':'bad'},403,'foreign owner'),(1,2,{'notes':'bad'},404,'foreign org'),(1,1,{'invented_privileged_field':True},403,'raw unknown key'),(1,1,{'printer_id':9},403,'forbidden printer'),(1,1,{'item_name':None},422,'null name'),(1,1,{'quantity':None},422,'null quantity')]:
 j=seed(owner,org);before=observe(j);patch(j,body,expected);assert observe(j)==before;checks.append(label)
for scope,expected in [('read:jobs',403),('write:jobs',200)]:
 raw='odin_owned_correction_'+scope
 with engine.begin() as c:c.execute(text('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(1,:name,:hash,:prefix,:scopes)'),{'name':'owned scoped key','hash':auth.hash_password(raw),'prefix':raw[:10],'scopes':json.dumps([scope])})
 j=seed();patch(j,{'notes':'scope'},expected,{'X-API-Key':raw});assert observe(j)['notes']==('scope' if expected==200 else None);checks.append('actual hashed API key '+scope)
# Real acquire commits first while eligible; retained fence blocks rejected correction.
j=seed(status=JobStatus.PENDING);claim=acquire(engine,9,j,'a'*64,'b'*64,'c'*64,1);assert claim.status=='acquired'
with engine.begin() as c:c.execute(text("UPDATE jobs SET status='rejected' WHERE id=:id"),{'id':j})
before=observe(j);patch(j,{'notes':'blocked'},409);assert observe(j)==before
with engine.connect() as c:assert c.execute(text('SELECT token FROM printer_dispatch_claims WHERE job_id=:id'),{'id':j}).scalar_one()==claim.token
checks.append('actual claim first retained untouched correction409')
# Exact route job lock held when real acquire runs; after commit rejected status refuses.
j=seed();original=Session.execute;seen=[]
def during_lock(self,statement,*args,**kwargs):
 result=original(self,statement,*args,**kwargs)
 if str(statement)=='SELECT id FROM jobs WHERE id=:id FOR UPDATE NOWAIT':seen.append(acquire(engine,9,j,'d'*64,'e'*64,'f'*64,1).status)
 return result
Session.execute=during_lock
try:patch(j,{'notes':'locked correction'},200)
finally:Session.execute=original
assert seen==['uncertain'];assert acquire(engine,9,j,'d'*64,'e'*64,'f'*64,1).status=='refused'
checks.append('actual edit lock first acquire NOWAIT uncertain then refused after commit')
j=seed()
with engine.connect() as held:
 tx=held.begin();held.execute(text('SELECT id FROM jobs WHERE id=:id FOR UPDATE'),{'id':j});patch(j,{'notes':'blocked'},503);assert observe(j)['notes'] is None;tx.rollback()
patch(j,{'notes':'released'},200);checks.append('real PG job NOWAIT contention and release')
# Fault raised only at real outer commit; observer must see all four original values.
j=seed();before=observe(j)
with engine.begin() as c:
 c.exec_driver_sql("CREATE FUNCTION owned_correction_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned deferred fault' USING ERRCODE='23514'; END $$")
 c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_correction_fault AFTER UPDATE ON jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_correction_fault()')
patch(j,{'item_name':'not committed','quantity':7,'notes':'not committed','due_date':'2026-12-02T10:30:00'},500);assert observe(j)==before
with engine.begin() as c:
 c.exec_driver_sql('DROP TRIGGER owned_correction_fault ON jobs');c.exec_driver_sql('DROP FUNCTION owned_correction_fault()')
patch(j,{'notes':'after fault removal'},200);checks.append('real deferred COMMIT fault atomic rollback fresh observer')
print(json.dumps({'checks':checks,'registered_bootstrap':'current026','actual_http_auth':'signed JWT and real hashed API keys; get_db fixture only; real EducationPolicyService','passed':True}),flush=True)
engine.dispose();configured_db.engine.dispose()
print('rejected-submitter-correction-native-pass',flush=True)
