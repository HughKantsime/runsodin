"""Actual public branding reads and administrator update/persistence boundaries."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")

@pytest.fixture
def branding_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system.routes_settings import router
    app=FastAPI();app.include_router(router)
    def db():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=db
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','is_active':True,'group_id':None}
    return TestClient(app)


def test_branding_public_defaults_and_admin_partial_update_persist(branding_client):
    before=branding_client.get('/branding')
    assert before.status_code==200
    assert before.json()['app_name']=='O.D.I.N.'
    update={'app_name':'Synthetic School','primary_color':'#123abc','support_url':'https://support.example.invalid/help'}
    response=branding_client.put('/branding',json=update)
    assert response.status_code==200,response.text
    saved=branding_client.get('/branding').json()
    for key,value in update.items():assert saved[key]==value
    assert saved['accent_color']==before.json()['accent_color']
    assert saved['updated_at'] is not None


@pytest.mark.parametrize('bad',[{'primary_color':'red;display:none'}, {'app_name':None}, {'app_name':'x'*101}, {'support_url':'javascript:alert(1)'}])
def test_branding_invalid_update_rejected_atomically(branding_client,bad):
    before=branding_client.get('/branding').json()
    response=branding_client.put('/branding',json={'app_subtitle':'Must not persist',**bad})
    assert response.status_code==422,response.text
    assert branding_client.get('/branding').json()==before

@pytest.mark.parametrize('bad',[{'app_name':'   '},{'font_body':'url(https://example.invalid/font)'},{'font_mono':'monospace; color:red'},{'font_display':None},{'support_url':'https://'},{'support_url':'https://example.invalid/\nunsafe'},{'primary_color':'#abc'},{'footer_text':None}])
def test_branding_additional_invalid_fields_are_atomic(branding_client,bad):
    before=branding_client.get('/branding').json()
    assert branding_client.put('/branding',json={'app_subtitle':'Must not persist',**bad}).status_code==422
    assert branding_client.get('/branding').json()==before

@pytest.mark.parametrize('clear',['','   ',None])
def test_branding_optional_text_and_support_removal_remain_supported(branding_client,clear):
    assert branding_client.put('/branding',json={'support_url':'https://example.invalid/help'}).status_code==200
    response=branding_client.put('/branding',json={'support_url':clear,'app_subtitle':'','footer_text':'','font_body':'"JetBrains Mono", ui-monospace, monospace'})
    assert response.status_code==200,response.text
    saved=branding_client.get('/branding').json()
    assert saved['support_url'] is None
    assert saved['app_subtitle']==saved['footer_text']==''
    assert saved['font_body']=='"JetBrains Mono", ui-monospace, monospace'


def test_all_default_branding_fields_accept_full_save(branding_client):
    from modules.organizations.branding import UPDATABLE_FIELDS
    before=branding_client.get('/branding').json()
    response=branding_client.put('/branding',json={key:before[key] for key in UPDATABLE_FIELDS})
    assert response.status_code==200,response.text
    assert all(response.json()[key]==before[key] for key in UPDATABLE_FIELDS)


def test_branding_read_is_public_without_identity(branding_client):
    from core.dependencies import get_current_user
    def forbidden_identity():
        raise AssertionError('Public branding must not require authentication')
    branding_client.app.dependency_overrides[get_current_user]=forbidden_identity
    assert branding_client.get('/branding').status_code==200


@pytest.mark.parametrize('identity',[{'id':1,'role':'viewer','group_id':None},{'id':3,'role':'admin','group_id':1},None])
def test_branding_global_update_denies_non_superadmin_without_mutation(branding_client,identity):
    from core.dependencies import get_current_user
    before=branding_client.get('/branding').json()
    branding_client.app.dependency_overrides[get_current_user]=lambda:identity
    response=branding_client.put('/branding',json={'app_name':'Must not persist'})
    assert response.status_code in {401,403}
    assert branding_client.get('/branding').json()==before


@pytest.mark.parametrize('scopes',[[],['read']])
def test_branding_update_rejects_scoped_credentials_without_write(branding_client,scopes):
    from core.dependencies import get_current_user
    before=branding_client.get('/branding').json()
    branding_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':scopes}
    response=branding_client.put('/branding',json={'app_name':'Must not persist'})
    assert response.status_code==403,response.text
    assert branding_client.get('/branding').json()==before

@pytest.mark.parametrize('method,path',[('POST','/branding/logo'),('POST','/branding/favicon'),('DELETE','/branding/logo'),('DELETE','/branding/favicon')])
@pytest.mark.parametrize('scopes',[[],['read']])
def test_branding_asset_mutators_require_write_scope_before_file_access(branding_client,scopes,method,path,monkeypatch):
    from core.dependencies import get_current_user
    from modules.system import routes_settings
    before=branding_client.get('/branding').json()
    branding_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':scopes}
    def forbidden(*args,**kwargs):raise AssertionError('Denied request must not inspect or create asset files')
    monkeypatch.setattr(routes_settings.os,'makedirs',forbidden)
    monkeypatch.setattr(routes_settings.os.path,'exists',forbidden)
    options={'files':{'file':('synthetic.png',b'not an image','image/png')}} if method=='POST' else {}
    response=branding_client.request(method,path,**options)
    assert response.status_code==403,response.text
    assert branding_client.get('/branding').json()==before


def test_branding_write_scoped_superadmin_can_save(branding_client):
    from core.dependencies import get_current_user
    branding_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'_auth_kind':'user_api_token','_token_scopes':['write']}
    response=branding_client.put('/branding',json={'app_name':'Authorized synthetic brand'})
    assert response.status_code==200,response.text
    assert branding_client.get('/branding').json()['app_name']=='Authorized synthetic brand'
