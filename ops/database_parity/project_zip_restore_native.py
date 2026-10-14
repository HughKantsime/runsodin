"""Owned current PostgreSQL project ZIP v2 export/import round trip via real JWT HTTP."""
import hashlib,io,json,os,stat,secrets,tempfile,zipfile
from pathlib import Path
private=Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(private)
storage=Path(tempfile.mkdtemp(prefix='owned-print-files-'))
os.environ['PRINT_FILE_STORAGE_ROOT']=str(storage)
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
bootstrap_database(engine,Path('/source/backend'))
from core import auth
source=storage/'owned.3mf';source.write_bytes(b'owned-postgres-project-bytes')
with engine.begin() as conn:
    conn.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned',TRUE),(2,'Other',TRUE)"))
    conn.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(71,'project-operator',:hash,'operator',TRUE,1)"),{'hash':auth.hash_password(secrets.token_urlsafe(32))})
    conn.execute(text("INSERT INTO projects(id,name,org_id,created_by,color) VALUES(101,'Owned project',1,71,'#112233'),(102,'Foreign project',2,71,'#112233')"))
    conn.execute(text("INSERT INTO print_archives(id,print_name,status,org_id,ownership_state,ownership_source,project_id,file_path,completed_at,tags,filament_used_grams) "
                      "VALUES(201,'Owned archive','completed',1,'known','synthetic_owner',101,:path,'2026-10-01 12:00:00+00','x,y',4.5),"
                      "(202,'Foreign archive','completed',2,'known','synthetic_owner',101,NULL,NULL,'',NULL)"),{'path':str(source)})
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from core.db import get_db
from core.audit_actor import register_request_session,clear_session
from core.middleware.audit_actor import AuditActorMiddleware
from modules.archives.routes import projects
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(projects.router)
def database(request:Request):
    with Session(engine,autoflush=False) as db:
        register_request_session(db,request)
        try:yield db
        finally:clear_session(db)
app.dependency_overrides[get_db]=database
headers={'Authorization':'Bearer '+auth.create_access_token({'sub':'project-operator'})}
with TestClient(app) as client:
    exported=client.get('/projects/101/export',headers=headers)
    assert exported.status_code==200,exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as package:
        manifest=json.loads(package.read('project.json'))
        assert [a['display']['print_name'] for a in manifest['archives']]==['Owned archive']
        assert package.read('files/000.3mf')==source.read_bytes()
    print('postgres-project-export-visibility-pass',flush=True)
    imported=client.post('/projects/import',headers=headers,files={'file':('p.zip',exported.content,'application/zip')})
    assert imported.status_code==200,imported.text
    project=imported.json()['id'];assert project not in (101,102)
    with engine.connect() as conn:
        rows=conn.execute(text('SELECT print_name,org_id,user_id,file_path,file_hash,tags,filament_used_grams,ownership_source,accounting_state FROM print_archives WHERE project_id=:p'),{'p':project}).all()
    assert len(rows)==1
    row=rows[0]
    assert row.print_name=='Owned archive' and row.org_id==1 and row.user_id==71 and row.tags=='x,y' and row.filament_used_grams==4.5
    assert row.ownership_source=='project_import' and row.accounting_state=='unresolved'
    assert Path(row.file_path).read_bytes()==source.read_bytes() and row.file_hash==hashlib.sha256(source.read_bytes()).hexdigest()
    print('postgres-project-import-restore-pass',flush=True)
    tampered=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(exported.content)) as old,zipfile.ZipFile(tampered,'w') as new:
        new.writestr('project.json',old.read('project.json'));new.writestr('files/000.3mf',b'X'*len(old.read('files/000.3mf')))
    refused=client.post('/projects/import',headers=headers,files={'file':('p.zip',tampered.getvalue(),'application/zip')})
    assert refused.status_code==400
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM projects')).scalar_one()==3
    assert len(list((storage/'project-imports').iterdir()))==1
    print('postgres-project-import-tamper-refusal-pass',flush=True)
engine.dispose()
print('project-zip-restore-native-pass',flush=True)
