"""Real opaque credentials cannot mutate deployment-wide branding without write."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def branding_token_client(token_client,review_db):
    from modules.system.routes_settings import router
    client,issue=token_client;client.app.include_router(router)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
    return client,issue

@pytest.mark.parametrize('scopes',[[],['read'],['write:jobs'],['write:printers']])
@pytest.mark.parametrize('method,path',[('PUT','/branding'),('POST','/branding/logo'),('POST','/branding/favicon'),('DELETE','/branding/logo'),('DELETE','/branding/favicon')])
def test_actual_narrow_token_denied_before_branding_mutation(branding_token_client,scopes,method,path):
    client,issue=branding_token_client
    before=client.get('/branding').json()
    options={'json':{'app_name':'Must not persist'}} if method=='PUT' else {'files':{'file':('synthetic.png',b'not an image','image/png')}} if method=='POST' else {}
    response=client.request(method,path,headers=issue(scopes),**options)
    assert response.status_code in ({401,403} if not scopes else {403}),response.text
    assert client.get('/branding').json()==before

@pytest.mark.parametrize('scope',['write','admin'])
def test_actual_broad_token_can_update_branding(branding_token_client,scope):
    client,issue=branding_token_client
    response=client.put('/branding',headers=issue([scope]),json={'app_name':'Authorized synthetic brand'})
    assert response.status_code==200,response.text
    assert client.get('/branding').json()['app_name']=='Authorized synthetic brand'
