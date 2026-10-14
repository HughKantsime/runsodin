"""Actual PG HTTP recent projection; minimal owned tables, no application data."""
import os
import secrets
os.environ.setdefault('JWT_SECRET_KEY',secrets.token_urlsafe(48))
from datetime import datetime,timezone
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.db import get_db
from core.dependencies import get_current_user
from core.registry import registry
from modules.jobs.recent_terminal import router
from modules.organizations.education_review_service import archive_audience_sql,assert_file_preview_allowed


def main():
    engine=create_engine(os.environ['RECENT_TERMINAL_NATIVE_URL'])
    with engine.begin() as db:
        statements=[
            'CREATE TABLE jobs(id INTEGER PRIMARY KEY,item_name TEXT,status TEXT,charged_to_org_id INTEGER,model_id INTEGER,print_file_id INTEGER,printer_id INTEGER,actual_start TIMESTAMP,actual_end TIMESTAMP,fail_reason TEXT,rejected_reason TEXT)',
            'CREATE TABLE models(id INTEGER PRIMARY KEY,name TEXT,org_id INTEGER,print_file_id INTEGER)',
            'CREATE TABLE print_files(id INTEGER PRIMARY KEY,org_id INTEGER,stored_path TEXT,created_by INTEGER,model_id INTEGER,job_id INTEGER,thumbnail_b64 TEXT)',
            'CREATE TABLE printers(id INTEGER PRIMARY KEY,name TEXT,org_id INTEGER,shared BOOLEAN)',
            'CREATE TABLE users(id INTEGER PRIMARY KEY,group_id INTEGER,username TEXT)',
            'CREATE TABLE education_cost_centers(id INTEGER PRIMARY KEY,org_id INTEGER,state TEXT)',
            'CREATE TABLE education_cost_center_grants(org_id INTEGER,cost_center_id INTEGER,user_id INTEGER,role TEXT,state TEXT)',
            'CREATE TABLE education_upload_operations(operation_id TEXT PRIMARY KEY,org_id INTEGER,user_id INTEGER,final_path TEXT,state TEXT)',
            'CREATE TABLE education_submissions(id INTEGER PRIMARY KEY,org_id INTEGER,job_id INTEGER,print_file_id INTEGER,model_id INTEGER,operation_id TEXT,submitted_by INTEGER,cost_center_id INTEGER,status TEXT,approved_printer_id INTEGER,approved_by INTEGER,lifecycle_revision INTEGER,compatibility_engine_version TEXT,created_at TIMESTAMP,updated_at TIMESTAMP)',
        ]
        for statement in statements:db.execute(text(statement))
        for identifier in range(100,110):
            db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,actual_start,actual_end) VALUES(:id,:name,'completed',1,:start,:end)"),
                {'id':identifier,'name':f'Native terminal {identifier}','start':datetime(2026,10,identifier-90,10), 'end':datetime(2026,10,identifier-90,12)})
        db.execute(text("INSERT INTO users VALUES(1,1,'owner'),(4,1,'classmate')"))
        db.execute(text("INSERT INTO education_cost_centers VALUES(7,1,'active')"))
        db.execute(text("INSERT INTO print_files VALUES(11,1,'/opaque/educational',1,12,NULL,NULL)"))
        db.execute(text("INSERT INTO models VALUES(12,'Educational model',1,11)"))
        db.execute(text("INSERT INTO education_upload_operations VALUES('owned',1,1,'/opaque/educational','committed')"))
        db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,model_id,print_file_id,actual_start,actual_end) VALUES(13,'Private newest educational','completed',1,12,11,'2026-10-20 10:00:00','2026-10-20 12:00:00')"))
        db.execute(text("INSERT INTO education_submissions(id,org_id,job_id,print_file_id,model_id,operation_id,submitted_by,cost_center_id,status,lifecycle_revision) VALUES(14,1,13,11,12,'owned',1,7,'submitted',1)"))
    provider=SimpleNamespace(archive_audience_sql=archive_audience_sql,assert_file_preview_allowed=assert_file_preview_allowed)
    original=registry.get_provider;registry.get_provider=lambda name:provider if name=='EducationPolicyProvider' else original(name)
    principal={'id':4,'group_id':1,'role':'viewer','_auth_kind':'user_api_token','_token_scopes':['read:jobs']}
    app=FastAPI();app.include_router(router,prefix='/api')
    app.dependency_overrides[get_current_user]=lambda:principal
    def database():
        with Session(engine) as db:yield db
    app.dependency_overrides[get_db]=database
    try:
        with TestClient(app) as client:
            result=client.get('/api/jobs/recent-terminal');assert result.status_code==200,result.text
            assert [row['id'] for row in result.json()]==list(range(109,101,-1))
            assert all(row['duration_seconds']==7200 for row in result.json())
            assert all(row['actual_end'].endswith('Z') for row in result.json())
            print('recent-native-audience-before-limit-eight-UTC-pass')
            with engine.begin() as db:db.execute(text("UPDATE jobs SET actual_end='2026-10-21 12:00:00' WHERE id IN(100,101)"))
            assert [row['id'] for row in client.get('/api/jobs/recent-terminal').json()][:2]==[101,100]
            principal['id']=1
            assert any(row['id']==13 for row in client.get('/api/jobs/recent-terminal').json())
            principal['id']=4
            with engine.begin() as db:
                db.execute(text('DELETE FROM jobs WHERE id>=100'))
                db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,actual_start,actual_end) VALUES(101,'Null legacy','failed',1,NULL,NULL),(102,'Infinite legacy','completed',1,NULL,'infinity'),(103,'Zero actual','completed',1,'2026-10-13 12:00:00','2026-10-13 12:00:00')"))
            result=client.get('/api/jobs/recent-terminal');assert result.status_code==200,result.text
            assert [row['id'] for row in result.json()]==[103,102,101]
            assert result.json()[0]['duration_seconds']==0
            assert all(row['actual_end'] is None and row['duration_seconds'] is None for row in result.json()[1:])
            print('recent-native-ties-null-infinite-legacy-zero-pass')
    finally:registry.get_provider=original;engine.dispose()
    print('recent-terminal-native-pass')

if __name__=='__main__':main()
