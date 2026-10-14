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
