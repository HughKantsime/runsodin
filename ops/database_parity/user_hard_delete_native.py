"""Owned current PostgreSQL admin user hard delete via real JWT HTTP."""
import os,stat,secrets
from pathlib import Path
private=Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(private)
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
bootstrap_database(engine,Path('/source/backend'))
from core import auth
with engine.begin() as conn:
    conn.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned',TRUE)"))
    for identifier,name,role in [(71,'delete-admin','admin'),(72,'deletable','operator'),(73,'with-history','operator')]:
        conn.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(:id,:n,:h,:r,TRUE,1)"),{'id':identifier,'n':name,'h':auth.hash_password(secrets.token_urlsafe(24)),'r':role})
    for uid in (72,73):
        conn.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES(:u,:j)"),{'u':uid,'j':f'jti-{uid}'})
        conn.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,'t','h','odin_x','[]')"),{'u':uid})
    conn.execute(text("INSERT INTO jobs(item_name,status,submitted_by) VALUES('Kept','completed',73)"))
    conn.execute(text("SELECT setval(pg_get_serial_sequence('users','id'),100)"))
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from core.db import get_db
from core.audit_actor import register_request_session,clear_session
from core.middleware.audit_actor import AuditActorMiddleware
from modules.organizations import routes_users
import license_manager
license_manager.require_feature=lambda *a,**k:None
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(routes_users.router)
def database(request:Request):
    with Session(engine,autoflush=False) as db:
        register_request_session(db,request)
        try:yield db
        finally:clear_session(db)
app.dependency_overrides[get_db]=database
owner={'Authorization':'Bearer '+auth.create_access_token({'sub':'delete-admin','role':'admin'})}
old={'Authorization':'Bearer '+auth.create_access_token({'sub':'deletable','role':'operator'})}
with TestClient(app) as client:
    refused=client.delete('/users/73',headers=owner)
    assert refused.status_code==409 and 'jobs' in refused.json()['detail'],refused.text
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM active_sessions WHERE user_id=73")).scalar_one()==1
        assert conn.execute(text("SELECT COUNT(*) FROM token_blacklist WHERE jti='jti-73'")).scalar_one()==0
    print('postgres-user-delete-history-refusal-pass',flush=True)
    deleted=client.delete('/users/72',headers=owner)
    assert deleted.status_code==200,deleted.text
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM users WHERE id=72")).scalar_one()==0
        assert conn.execute(text("SELECT COUNT(*) FROM active_sessions WHERE user_id=72")).scalar_one()==0
        assert conn.execute(text("SELECT COUNT(*) FROM api_tokens WHERE user_id=72")).scalar_one()==0
        assert conn.execute(text("SELECT COUNT(*) FROM token_blacklist WHERE jti='jti-72'")).scalar_one()==1
    print('postgres-user-delete-credential-revocation-pass',flush=True)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id,created_at) VALUES('deletable',:h,'admin',TRUE,1,CURRENT_TIMESTAMP+INTERVAL '5 seconds')"),{'h':auth.hash_password(secrets.token_urlsafe(24))})
    assert client.get('/users',headers=old).status_code==401
    print('postgres-user-delete-username-reuse-token-pass',flush=True)
engine.dispose()
print('user-hard-delete-native-pass',flush=True)
