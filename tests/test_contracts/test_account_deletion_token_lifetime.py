"""Real login/deletion/recreation contract; isolated signed tokens and database."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_user_limits import signed_license


def test_deleted_account_token_cannot_authorize_replacement_username(review_db,signed_license,monkeypatch):
    import core.auth as auth
    import license_manager
    from core.db import get_db
    from modules.organizations import routes_auth,routes_users
    monkeypatch.setattr(auth,'SECRET_KEY','owned-account-lifetime-test-key-only')
    manager,license=signed_license();assert license.valid
    monkeypatch.setattr(license_manager,'get_license',manager.get_license)
    db=review_db
    password='OwnedFixtureLogin!123'
    db.execute(text('INSERT INTO users(id,username,email,password_hash,role,is_active,group_id) VALUES(30,:name,:email,:hash,\'viewer\',1,1)'),{'name':'reused-fixture','email':'reused@example.test','hash':auth.hash_password(password)})
    db.commit()
    app=FastAPI();app.include_router(routes_auth.router);app.include_router(routes_users.router)
    def database():
        with Session(db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=database
    with TestClient(app) as client:
        login=client.post('/auth/login',data={'username':'reused-fixture','password':password})
        assert login.status_code==200
        token=login.json()['access_token']
        client.cookies.clear()
        old={'Authorization':'Bearer '+token}
        assert client.get('/users',headers=old).status_code==403
        owner={'Authorization':'Bearer '+auth.create_access_token({'sub':'admin','role':'admin'})}
        deleted=client.delete('/users/30',headers=owner)
        assert deleted.status_code==200
        assert client.get('/users',headers=old).status_code==401
        created=client.post('/users',headers=owner,json={'username':'reused-fixture','email':'replacement@example.test','password':password,'role':'admin','group_id':1,'send_welcome_email':False})
        assert created.status_code==200
        # Same username is now a different account. A real old login may not
        # regain authority or inherit the replacement administrator's role.
        assert client.get('/users',headers=old).status_code==401


@pytest.fixture
def deletion_case(review_db,signed_license,monkeypatch):
    import core.auth as auth
    import license_manager
    from core.db import get_db
    from modules.organizations import routes_auth,routes_users
    monkeypatch.setattr(auth,'SECRET_KEY','owned-account-lifetime-test-key-only')
    manager,license=signed_license();assert license.valid
    monkeypatch.setattr(license_manager,'get_license',manager.get_license)
    review_db.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,group_id) VALUES(31,'doomed','doomed@example.test',:h,'operator',1,1)"),{'h':auth.hash_password('OwnedFixtureLogin!123')})
    review_db.commit()
    app=FastAPI();app.include_router(routes_auth.router);app.include_router(routes_users.router)
    def database():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=database
    owner={'Authorization':'Bearer '+auth.create_access_token({'sub':'admin','role':'admin'})}
    with TestClient(app) as client:
        yield client,owner,review_db,auth


CREDENTIALS=[('api_tokens',"INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(31,'t','h','odin_x','[]')"),
             ('active_sessions',"INSERT INTO active_sessions(user_id,token_jti) VALUES(31,'doomed-jti')"),
             ('push_subscriptions',None),('alert_preferences',None),('password_reset_tokens',None),('biometric_tokens',None),('push_devices',None)]


def _count(db,table):
    return db.execute(text(f'SELECT COUNT(*) FROM {table} WHERE user_id=31')).scalar_one()


def test_delete_revokes_every_credential_atomically(deletion_case):
    client,owner,db,_=deletion_case
    for _,sql in CREDENTIALS:
        if sql:db.execute(text(sql))
    db.commit()
    assert client.post('/auth/login',data={'username':'doomed','password':'OwnedFixtureLogin!123'}).status_code==200
    client.cookies.clear()
    jtis=[r[0] for r in db.execute(text('SELECT token_jti FROM active_sessions WHERE user_id=31'))]
    assert len(jtis)==2
    response=client.delete('/users/31',headers=owner)
    assert response.status_code==200,response.text
    db.expire_all()
    for table,_ in CREDENTIALS:assert _count(db,table)==0,table
    assert db.execute(text('SELECT COUNT(*) FROM users WHERE id=31')).scalar_one()==0
    blacklisted={r[0] for r in db.execute(text('SELECT jti FROM token_blacklist'))}
    assert set(jtis)<=blacklisted


def test_delete_with_retained_history_is_refused_without_side_effects(deletion_case):
    client,owner,db,_=deletion_case
    db.execute(text("INSERT INTO jobs(item_name,status,submitted_by) VALUES('Kept history','completed',31)"))
    db.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES(31,'kept-jti')"));db.commit()
    response=client.delete('/users/31',headers=owner)
    assert response.status_code==409 and 'jobs' in response.json()['detail'] and 'erase' in response.json()['detail']
    db.expire_all()
    assert db.execute(text('SELECT COUNT(*) FROM users WHERE id=31')).scalar_one()==1
    assert _count(db,'active_sessions')==1
    assert db.execute(text("SELECT COUNT(*) FROM token_blacklist WHERE jti='kept-jti'")).scalar_one()==0


def test_token_issued_before_account_creation_never_authorizes_it(deletion_case):
    import jwt
    client,owner,db,auth=deletion_case
    from datetime import datetime,timedelta,timezone
    stale=jwt.encode({'sub':'doomed','exp':datetime.now(timezone.utc)+timedelta(hours=1),'iat':datetime.now(timezone.utc)-timedelta(hours=2),'jti':'stale'},auth.SECRET_KEY,algorithm=auth.ALGORITHM)
    db.execute(text('UPDATE users SET created_at=:now WHERE id=31'),{'now':(datetime.now(timezone.utc)-timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')});db.commit()
    assert client.get('/users',headers={'Authorization':'Bearer '+stale}).status_code==401
    fresh=auth.create_access_token({'sub':'doomed','role':'operator'})
    assert client.get('/users',headers={'Authorization':'Bearer '+fresh}).status_code==403
