"""Actual HTTP license mutation gates; external operations stop at owned sentinel."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from tests.test_contracts.test_license_offline_import import owned_import

ENDPOINTS=[
 ('DELETE','/license','remove_license',None),
 ('POST','/license/activate','activate_license',{'key':'synthetic-key'}),
 ('POST','/license/unactivate','unactivate_license',{'key':'synthetic-key'}),
 ('POST','/license/reactivate','reactivate_license',None),
 ('POST','/license/activation-request','build_activation_request',{'key':'synthetic-key','nonce':'synthetic-nonce'}),
]

@pytest.fixture
def authority_client(owned_import,monkeypatch):
    from modules.system import routes_health as routes
    from core.dependencies import get_current_user
    from core.db import get_db
    upload,path,signed,audit,db,lm,sign=owned_import
    reached=[]
    def stop_before_external_or_device_operation():
        reached.append(True)
        raise HTTPException(status_code=418,detail='Owned external boundary reached')
    monkeypatch.setattr(routes,'get_installation_id',stop_before_external_or_device_operation)
    monkeypatch.setattr(routes,'get_license',lambda:SimpleNamespace(key='synthetic-key', device_bound=False))
    monkeypatch.setenv('ODIN_LICENSE_READ_ONLY','0')
    app=FastAPI()
    for method,url,name,body in ENDPOINTS: app.add_api_route(url,getattr(routes,name),methods=[method])
    principal={'id':1,'role':'admin','group_id':None}
    app.dependency_overrides[get_current_user]=lambda:principal
    app.dependency_overrides[get_db]=lambda:db
    return TestClient(app),principal,reached,path,signed,audit,db

@pytest.mark.parametrize('endpoint',ENDPOINTS)
@pytest.mark.parametrize('identity',[
 {'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':['read']},
 {'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':[]},
 {'role':'admin','group_id':7},
 {'role':'viewer','group_id':None},
])
def test_mutation_denies_insufficient_authority(authority_client,endpoint,identity):
    client,principal,reached,path,signed,audit,db=authority_client
    principal.update(identity)
    method,url,name,body=endpoint
    with client: response=client.request(method,url,json=body)
    assert response.status_code==403
    assert reached==[] and path.read_text()==signed
    audit.assert_not_called();db.commit.assert_not_called()

@pytest.mark.parametrize('endpoint',ENDPOINTS)
@pytest.mark.parametrize('identity',[
 {'_auth_kind':'user_api_token','_token_scopes':['write']},
 {'_auth_kind':'session','_token_scopes':[]},
])
def test_system_admin_with_write_authority_reaches_owned_boundary(authority_client,endpoint,identity):
    client,principal,reached,path,signed,audit,db=authority_client
    principal.update(identity)
    method,url,name,body=endpoint
    with client: response=client.request(method,url,json=body)
    if method=='DELETE':
        assert response.status_code==200 and not path.exists()
        audit.assert_called_once();db.commit.assert_called_once()
    else:
        assert response.status_code==418 and reached==[True]
        assert path.read_text()==signed

@pytest.mark.parametrize('endpoint',ENDPOINTS[:4])
def test_managed_installation_denies_existing_mutation_routes(authority_client,monkeypatch,endpoint):
    client,principal,reached,path,signed,audit,db=authority_client
    monkeypatch.setenv('ODIN_LICENSE_READ_ONLY','1')
    principal.update({'_auth_kind':'user_api_token','_token_scopes':['write']})
    method,url,name,body=endpoint
    with client: response=client.request(method,url,json=body)
    assert response.status_code==403 and reached==[]
    assert path.read_text()==signed
    audit.assert_not_called();db.commit.assert_not_called()
