"""Owned current PostgreSQL report query and real JWT HTTP controls."""
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
from core.base import JobStatus
from core.economic_identity import _add_created
from modules.jobs.models import Job
from modules.reporting.education_usage import build_education_usage_report
from datetime import datetime,timedelta,timezone
with engine.begin() as conn:
    conn.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned',TRUE),(2,'Other',TRUE)"))
    for identifier,username,org in [(71,'report-admin',1),(72,'transferred',2),(73,'platform',None)]:
        conn.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(:id,:name,:hash,'admin',TRUE,:org)"),{'id':identifier,'name':username,'hash':auth.hash_password(secrets.token_urlsafe(32)),'org':org})
end=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(hours=1)
with Session(engine) as db:
    for actual,quantity in [(2,4),(3,1)]:
        _add_created(db,Job(item_name='Owned report',status=JobStatus.COMPLETED,submitted_by=71,charged_to_org_id=1,quantity=quantity,duration_hours=1,actual_start=end-timedelta(hours=actual),actual_end=end,scheduled_start=end-timedelta(hours=1),scheduled_end=end),'job',owner_org_id=1)
    _add_created(db,Job(item_name='Retained other history',status=JobStatus.COMPLETED,submitted_by=72,charged_to_org_id=2,duration_hours=999,quantity=1),'job',owner_org_id=2)
    db.commit()
    db.execute(text('UPDATE users SET group_id=1 WHERE id=72'));db.commit()
    tenant=build_education_usage_report(30,db,{'id':71,'role':'admin','group_id':1})
    assert tenant['summary']['total_jobs']==2
    assert tenant['summary']['total_print_hours']==5 and tenant['summary']['estimated_print_hours']==5
    assert tenant['summary']['accuracy']==41.7 and tenant['summary']['accuracy_samples']==2
    assert tenant['summary']['total_cost'] is None and tenant['summary']['currency'] is None
    assert {r['user_id'] for r in tenant['users']}=={71}
    print('postgres-owned-report-snapshot-and-transferred-owner-pass',flush=True)
    db.execute(text("INSERT INTO jobs(item_name,status,submitted_by,charged_to_org_id,duration_hours,quantity) VALUES('Unknown maintenance','completed',73,NULL,999,1)"));db.commit()
    platform=build_education_usage_report(30,db,{'id':73,'role':'admin','group_id':None})
    assert platform['summary']['unproven_history_jobs']==1
    assert platform['summary']['total_print_hours']==5
    assert platform['summary']['estimated_print_hours']==1004
    print('postgres-platform-unproven-count-only-pass',flush=True)
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from core.db import get_db
from core.audit_actor import register_request_session,clear_session
from core.middleware.audit_actor import AuditActorMiddleware
from modules.reporting.routes import analytics
from unittest.mock import patch
app=FastAPI();app.add_middleware(AuditActorMiddleware);app.include_router(analytics.router)
def database(request:Request):
    with Session(engine,autoflush=False) as db:
        register_request_session(db,request)
        try:yield db
        finally:clear_session(db)
app.dependency_overrides[get_db]=database
with patch.object(analytics,'require_feature',lambda _:None),TestClient(app) as client:
    response=client.get('/education/usage-report',headers={'Authorization':'Bearer '+auth.create_access_token({'sub':'report-admin'})})
    assert response.status_code==200
    result=response.json()
    assert result['summary']['total_jobs']==2 and {r['user_id'] for r in result['users']}=={71}
    assert result['summary']['accuracy']==41.7
print('postgres-real-jwt-report-pass',flush=True)
engine.dispose()
print('report-usage-native-pass',flush=True)
