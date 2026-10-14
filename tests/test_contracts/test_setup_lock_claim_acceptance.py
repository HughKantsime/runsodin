"""Completed onboarding mutation-lock acceptance with owned fresh databases."""
from pathlib import Path
import secrets
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.db import get_db
from core.dependencies import get_current_user
from core.schema import bootstrap_database
from modules.system import routes_setup

@pytest.fixture
def completed_setup(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'setup.db'),connect_args={'check_same_thread':False})
    bootstrap_database(engine,Path(__file__).resolve().parents[2]/'backend')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO system_config(key,value) VALUES('setup_complete','true')"))
    app=FastAPI();app.include_router(routes_setup.router)
    app.dependency_overrides[get_current_user]=lambda:{'id':1,'role':'admin','group_id':None}
    def database():
        with Session(engine) as db:yield db
    app.dependency_overrides[get_db]=database
    try:
        with TestClient(app) as client:yield client,engine
    finally:engine.dispose()

@pytest.mark.parametrize('path,body',[
    ('/setup/printer',{'name':'Synthetic','model':'A1'}),
    ('/setup/network',{'host_ip':'192.0.2.30'}),
    ('/setup/complete',{}),
])
def test_completed_setup_refuses_authenticated_mutations(completed_setup,path,body):
    client,_=completed_setup
    assert client.post(path,json=body).status_code==403


def test_completed_setup_stays_locked_without_users(completed_setup):
    client,engine=completed_setup
    response=client.post('/setup/admin',json={'username':'synthetic-new-owner','password':secrets.token_urlsafe(30)+'aA1!','email':'synthetic@example.test'})
    assert response.status_code==403
    with engine.connect() as conn: assert conn.execute(text('SELECT COUNT(*) FROM users')).scalar_one()==0


@pytest.mark.parametrize('existing_user',[False,True])
def test_uncompleted_setup_preserves_fresh_and_claimed_behavior(completed_setup,existing_user):
    client,engine=completed_setup
    with engine.begin() as conn:
        conn.execute(text("UPDATE system_config SET value='false' WHERE key='setup_complete'"))
        if existing_user:
            conn.execute(text("INSERT INTO users(username,password_hash,role) VALUES('synthetic-existing','unused','admin')"))
    response=client.post('/setup/admin',json={'username':'synthetic-new-owner','password':secrets.token_urlsafe(30)+'aA1!','email':'synthetic@example.test'})
    assert response.status_code==(403 if existing_user else 200)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM users')).scalar_one()==1
        if not existing_user:
            assert conn.execute(text("SELECT role FROM users WHERE username='synthetic-new-owner'")).scalar_one()=='admin'
