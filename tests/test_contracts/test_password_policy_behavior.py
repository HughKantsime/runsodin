"""Actual HTTP password policy; owned database and private mail sink only."""
import re
import secrets
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db,interactive_headers


@pytest.fixture
def policy_client(token_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    client,_=token_client;client.app.include_router(routes_users.router)
    review_db.get_bind().hide_parameters=True
    monkeypatch.setattr(routes_users,'check_user_limit',lambda _:None)
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:pytest.fail('Unexpected external mail entry'))
    return client


def count(db,table):
    assert table in ('users','audit_logs')
    with Session(db.get_bind()) as fresh:return fresh.execute(text('SELECT COUNT(*) FROM '+table)).scalar_one()


@pytest.mark.parametrize('weak',['Aa1','lowercase1','UPPERCASE1','NoDigitsHere'])
def test_create_weak_password_has_no_user_audit_or_mail(policy_client,review_db,weak):
    before=(count(review_db,'users'),count(review_db,'audit_logs'))
    response=policy_client.post('/users',headers=interactive_headers(),json={
        'username':'synthetic_policy_new','email':'policy@example.invalid','password':weak,'role':'viewer'})
    assert response.status_code==400
    assert (count(review_db,'users'),count(review_db,'audit_logs'))==before


def test_create_valid_password_salted_and_tenant_bound(policy_client,review_db):
    from core.auth import verify_password
    password=secrets.token_urlsafe(24)+'aA1'
    response=policy_client.post('/users',headers=interactive_headers(),json={
        'username':'synthetic_policy_new','email':'policy@example.invalid','password':password,'role':'viewer'})
    assert response.status_code==200
    assert response.json()=={'status':'created'}
    with Session(review_db.get_bind()) as fresh:
        row=fresh.execute(text("SELECT password_hash,group_id FROM users WHERE username='synthetic_policy_new'")).one()
        matched=verify_password(password,row.password_hash)
        assert matched and row.group_id==1
        is_salted=row.password_hash!=password
        assert is_salted
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='user.created'")).scalar_one()==1


@pytest.mark.parametrize("random_text",["a"*32,"A"*32,"1"*32,"_"*32])
def test_generated_welcome_password_enforces_classes_and_hashes_selected_value(policy_client,review_db,monkeypatch,random_text):
    from modules.organizations import routes_users
    from core.auth_helpers import _validate_password
    from core.auth import verify_password
    captured=[]
    requested=[]
    def random_source(size):requested.append(size);return random_text
    monkeypatch.setattr(secrets,'token_urlsafe',random_source)
    def mail(db,to,subject,body):
        selected=re.search(r'Temporary Password:</strong> ([^<]+)',body).group(1)
        valid=_validate_password(selected)[0]
        row=db.execute(text("SELECT password_hash FROM users WHERE username='synthetic_policy_new'")).scalar_one()
        matches=verify_password(selected,row)
        captured.append((valid,matches,to))
        return True
    monkeypatch.setattr(routes_users,'_send_odin_email',mail)
    response=policy_client.post('/users',headers=interactive_headers(),json={
        'username':'synthetic_policy_new','email':'policy@example.invalid','send_welcome_email':True,'role':'viewer'})
    assert response.status_code==200
    assert response.json()=={'status':'created','welcome_email':'sent'}
    assert captured==[(True,True,'policy@example.invalid')]
    assert requested==[24]


@pytest.mark.parametrize('weak',['Aa1','lowercase1','UPPERCASE1','NoDigitsHere'])
def test_update_weak_password_preserves_existing_hash_and_audit(policy_client,review_db,weak):
    with Session(review_db.get_bind()) as fresh:
        before=fresh.execute(text('SELECT password_hash FROM users WHERE id=1')).scalar_one()
    audits=count(review_db,'audit_logs')
    response=policy_client.patch('/users/1',headers=interactive_headers(),json={'password':weak})
    assert response.status_code==400
    with Session(review_db.get_bind()) as fresh:
        same=fresh.execute(text('SELECT password_hash FROM users WHERE id=1')).scalar_one()==before
        assert same
    assert count(review_db,'audit_logs')==audits


def test_foreign_tenant_create_remains_denied_before_policy(policy_client,review_db):
    before=count(review_db,'users')
    response=policy_client.post('/users',headers=interactive_headers(),json={
        'username':'synthetic_foreign','email':'foreign@example.invalid','password':'Aa1','group_id':2})
    assert response.status_code==403
    assert count(review_db,'users')==before


@pytest.mark.parametrize('kind',['missing','empty','welcome_without_email'])
def test_empty_password_creates_nothing(policy_client,review_db,kind):
    before=count(review_db,'users')
    body={'username':'synthetic_policy_new','email':'policy@example.invalid'}
    if kind=='empty':body['password']=''
    elif kind=='welcome_without_email':body.update(email='',send_welcome_email=True)
    response=policy_client.post('/users',headers=interactive_headers(),json=body)
    assert response.status_code==400
    assert count(review_db,'users')==before


@pytest.mark.parametrize('weak',['Aa1','lowercase1','UPPERCASE1','NoDigitsHere'])
def test_csv_weak_password_rejected_without_user(policy_client,review_db,weak):
    before=count(review_db,'users')
    csv='username,email,password,role\nsynthetic_policy_new,policy@example.invalid,'+weak+',viewer\n'
    response=policy_client.post('/users/import',headers=interactive_headers(),files={'file':('synthetic.csv',csv,'text/csv')})
    assert response.status_code==200
    assert response.json()['created']==0 and len(response.json()['errors'])==1
    assert count(review_db,'users')==before


def test_valid_create_audit_failure_rolls_back_without_mail(policy_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    from starlette.testclient import TestClient
    before=count(review_db,'users')
    def fail(*args,**kwargs):raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(routes_users,'log_audit',fail)
    with TestClient(policy_client.app,raise_server_exceptions=False) as client:
        response=client.post('/users',headers=interactive_headers(),json={
            'username':'synthetic_policy_new','email':'policy@example.invalid',
            'password':secrets.token_urlsafe(24)+'aA1','role':'viewer'})
    assert response.status_code==503
    assert count(review_db,'users')==before


@pytest.fixture
def initial_policy_client(tmp_path):
    from sqlalchemy import create_engine
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pathlib import Path
    from core.schema import bootstrap_database
    from core.db import get_db
    from modules.system import routes_setup
    engine=create_engine('sqlite:///'+str(tmp_path/'initial-policy.db'),hide_parameters=True)
    bootstrap_database(engine,Path(__file__).resolve().parents[2]/'backend')
    app=FastAPI();app.include_router(routes_setup.router)
    def database():
        with Session(engine) as db:yield db
    app.dependency_overrides[get_db]=database
    try:
        with TestClient(app) as client:yield client,engine
    finally:engine.dispose()


@pytest.mark.parametrize('weak',['Aa1','lowercase1','UPPERCASE1','NoDigitsHere'])
def test_initial_admin_obeys_same_password_policy(initial_policy_client,weak):
    client,engine=initial_policy_client
    response=client.post('/setup/admin',json={'username':'synthetic_initial','email':'initial@example.invalid','password':weak})
    assert response.status_code==400
    with engine.connect() as db:assert db.execute(text('SELECT COUNT(*) FROM users')).scalar_one()==0


def test_initial_admin_valid_password_hashed_and_second_setup_denied(initial_policy_client):
    from core.auth import verify_password
    client,engine=initial_policy_client;selected=secrets.token_urlsafe(24)+'aA1'
    response=client.post('/setup/admin',json={'username':'synthetic_initial','email':'initial@example.invalid','password':selected})
    assert response.status_code==200
    with engine.connect() as db:
        row=db.execute(text('SELECT password_hash,role FROM users')).one()
        verified=verify_password(selected,row.password_hash)
        assert verified and row.role=='admin'
    response=client.post('/setup/admin',json={'username':'synthetic_second','email':'second@example.invalid','password':selected})
    assert response.status_code==403
    with engine.connect() as db:assert db.execute(text('SELECT COUNT(*) FROM users')).scalar_one()==1


@pytest.mark.parametrize('length,expected',[(7,400),(8,200)])
def test_exact_minimum_password_length_http_boundary(policy_client,review_db,length,expected):
    selected='Aa1'+secrets.token_hex(3)[:length-3]
    response=policy_client.post('/users',headers=interactive_headers(),json={
        'username':'synthetic_minimum','email':'minimum@example.invalid','password':selected,'role':'viewer'})
    assert response.status_code==expected
    with Session(review_db.get_bind()) as fresh:
        rows=fresh.execute(text("SELECT password_hash FROM users WHERE username='synthetic_minimum'")).all()
        assert len(rows)==int(expected==200)
        if rows:
            from core.auth import verify_password
            matches=verify_password(selected,rows[0].password_hash)
            assert matches
