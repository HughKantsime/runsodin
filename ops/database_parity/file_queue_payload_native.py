"""Owned real PostgreSQL queue payload acceptance; never dispatches."""
import os,stat,json,hashlib,tempfile,zipfile
from pathlib import Path
os.environ['JWT_SECRET_KEY']='owned-queue-native-signing-key-only'
private=Path(os.environ['QUEUE_NATIVE_PASSWORD_FILE'])
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(private)
os.environ['PRINT_FILE_STORAGE_ROOT']='/tmp/owned-queue-storage'
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
from modules.models_library.routes import print_files
from modules.jobs.routes import jobs_crud
from modules.models_library import queue_payload
from core.registry import registry
from modules.organizations.services import EducationPolicyService
registry._providers['EducationPolicyProvider']=EducationPolicyService()
assert configured_db.IS_POSTGRES
with engine.begin() as c:
 c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'owned queue org',TRUE)"))
 c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id,quota_jobs) VALUES(3,'queueoperator','fixture','operator',TRUE,1,100)"))
 c.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,org_id) VALUES(9,'inert owned printer','fixture',TRUE,'bambu',1)"))
 c.execute(text("INSERT INTO models(id,name,org_id) VALUES(100,'owned model',1)"))
app=FastAPI();app.include_router(print_files.router,prefix='/api')
def database():
 with Session(engine) as db:yield db
app.dependency_overrides[get_db]=database
headers={'Authorization':'Bearer '+auth.create_access_token({'sub':'queueoperator','role':'operator'})}
checks=[];sequence=500
root=Path(tempfile.mkdtemp(prefix='owned-queue-payload-'))
def seed():
 global sequence
 sequence+=1;path=root/(str(sequence)+'.gcode.3mf')
 with zipfile.ZipFile(path,'w') as z:z.writestr('Metadata/plate_1.gcode','; owned inert payload')
 digest=hashlib.sha256(path.read_bytes()).hexdigest()
 with engine.begin() as c:c.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json,file_hash) VALUES(:id,100,'owned.gcode.3mf','owned.gcode.3mf',:path,1,'[]',:hash)"),{'id':sequence,'path':str(path),'hash':digest})
 return sequence,digest

def facts():
 with engine.connect() as c:
  return {'counts':tuple(c.execute(text('SELECT (SELECT count(*) FROM jobs),(SELECT count(*) FROM cost_identities),(SELECT quota_jobs FROM users WHERE id=3)')).one()),'quota_usage':[dict(r) for r in c.execute(text('SELECT * FROM quota_usage ORDER BY user_id,period_key')).mappings()], 'links':list(c.execute(text('SELECT id,job_id,file_hash FROM print_files ORDER BY id')).tuples())}
def assert_charge(before,after):
 prior=sum(r['jobs_used'] or 0 for r in before['quota_usage'] if r['user_id']==3)
 current=sum(r['jobs_used'] or 0 for r in after['quota_usage'] if r['user_id']==3)
 assert current==prior+1,('actual quota charge',prior,current)
def request(file,expected):
 with TestClient(app,raise_server_exceptions=False) as client:r=client.post('/api/print-files/'+str(file)+'/schedule?printer_id=9',headers=headers)
 assert r.status_code==expected,('generic status',expected,r.status_code)
 return r
for table,identifier in [('models',100),('printers',9)]:
 f,d=seed();before=facts()
 with engine.connect() as held:
  tx=held.begin();held.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE'),{'id':identifier});request(f,409);assert facts()==before;tx.rollback()
 r=request(f,200);assert_charge(before,facts())
 with engine.connect() as c:assert tuple(c.execute(text('SELECT print_file_id,payload_sha256,plate_index FROM jobs WHERE id=:id'),{'id':r.json()['job_id']}).one())==(f,d,0)
 checks.append('actual '+table+' NOWAIT409 effects unchanged; released positive exact binding/null fields')
# Same-transaction field mutation must be caught after factory, including NULL->value.
original=jobs_crud._stage_prospective_queue_values
for field in ['original_filename','compatible_api_types','bed_x_mm']:
 f,d=seed();before=facts()
 def drift(db,*args,**kwargs):
  job=original(db,*args,**kwargs)
  value=42 if field=='bed_x_mm' else 'changed'
  db.execute(text(f'UPDATE print_files SET {field}=:v WHERE id=:id'),{'v':value,'id':f});return job
 jobs_crud._stage_prospective_queue_values=drift
 try:request(f,409)
 finally:jobs_crud._stage_prospective_queue_values=original
 assert facts()==before
 with engine.connect() as c:assert c.execute(text(f'SELECT {field} FROM print_files WHERE id=:id'),{'id':f}).scalar_one()==('owned.gcode.3mf' if field=='original_filename' else None)
 checks.append('captured nullsafe '+field+' drift rollback')
f,d=seed();before=facts()
with engine.begin() as c:
 c.exec_driver_sql("CREATE FUNCTION owned_queue_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned commit fault' USING ERRCODE='23514'; END $$")
 c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_queue_fault AFTER INSERT ON jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_queue_fault()')
request(f,500);assert facts()==before
with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER owned_queue_fault ON jobs');c.exec_driver_sql('DROP FUNCTION owned_queue_fault()')
checks.append('actual deferred outer COMMIT failure rolls back job registry quota link')
# Studio uses actual multipart/upload, with a post-upload committed file boundary.
data=Path('/source/tests/fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes();digest=hashlib.sha256(data).hexdigest()
def studio(expected):
 print_files.limiter._storage.reset()
 with TestClient(app,raise_server_exceptions=False) as client:r=client.post('/api/prints',headers=headers,data={'target_printer_id':'9'},files={'file':('owned-studio.gcode.3mf',data,'application/octet-stream')})
 assert r.status_code==expected,('studio status',expected,r.status_code)
 return r
before=facts()
with engine.connect() as held:
 tx=held.begin();held.execute(text('SELECT id FROM printers WHERE id=9 FOR UPDATE'));studio(409);tx.rollback()
after=facts();assert after['counts']==before['counts'] and after['quota_usage']==before['quota_usage'] and len(after['links'])==len(before['links'])+1 and after['links'][-1][1] is None
checks.append('actual Studio target NOWAIT409 preserves committed upload only')
before=facts();r=studio(201);assert_charge(before,facts())
with engine.connect() as c:assert tuple(c.execute(text('SELECT print_file_id,payload_sha256,plate_index FROM jobs WHERE id=:id'),{'id':int(r.json()['id'])}).one())==(r.json()['file_id'],digest,0)
checks.append('actual Studio multipart positive exact binding and quota_usage jobs_used charge')
checks.append('fresh observer exact quota_usage rows/values preserved on contention, drift and deferred commit failure; successful generic and Studio requests each charge one')
print(json.dumps({'passed':True,'checks':checks,'scope':'actual current bootstrap PostgreSQL; actual JWT generic/Studio HTTP; no hardware or dispatch'}),flush=True)
import shutil
shutil.rmtree(root);engine.dispose();configured_db.engine.dispose()
print('file-queue-payload-native-pass',flush=True)
