"""Authenticated user projection must carry verified stable identity for browser isolation."""
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db,interactive_headers
import pytest

@pytest.fixture
def principal_client(token_client):
    from modules.organizations import routes_auth
    client,issue=token_client;client.app.include_router(routes_auth.router)
    return client,issue

@pytest.mark.parametrize('authentication',['token','interactive'])
def test_me_carries_verified_stable_id_and_tenant(principal_client,authentication):
    client,issue=principal_client
    headers=issue(['read:printers']) if authentication=='token' else interactive_headers()
    response=client.get('/auth/me',headers=headers)
    assert response.status_code==200
    assert response.json()['id']==3
    assert response.json()['group_id']==1
    assert response.json()['username']=='admin'
    assert not {'password_hash','token_hash','token','mfa_secret'} & response.json().keys()

def test_me_uses_current_database_role_with_same_stable_id(principal_client,review_db):
    client,issue=principal_client;headers=issue(['read:printers'])
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    response=client.get('/auth/me',headers=headers)
    assert response.status_code==200
    assert response.json()['id']==3 and response.json()['role']=='viewer'

def test_me_has_no_identity_without_authentication(principal_client):
    client,_=principal_client
    assert client.get('/auth/me').status_code==401
