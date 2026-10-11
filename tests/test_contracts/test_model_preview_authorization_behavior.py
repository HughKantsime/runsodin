"""Actual token/HTTP/database boundary checks for model geometry and previews."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def preview_client(token_client,review_db,monkeypatch):
    from modules.models_library.routes import print_files
    from modules.archives.routes import archives_crud
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client,issue=token_client
    client.app.include_router(print_files.router);client.app.include_router(archives_crud.router)
    mesh=json.dumps({'vertices':[0,0,0,1,0,0,0,1,0],'faces':[0,1,2]})
    review_db.execute(text('UPDATE print_files SET mesh_data=:mesh WHERE id=11'),{'mesh':mesh})
    review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
    for identifier,org in [(21,1),(22,2),(23,None)]:
        review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,mesh_data) VALUES(:id,:name,:name,:path,:org,:mesh)"),{'id':identifier,'name':f'synthetic-{identifier}.3mf','path':f'/opaque/synthetic-{identifier}.3mf','org':org,'mesh':mesh})
    for identifier,org,file in [(31,1,21),(32,2,22),(33,1,22),(34,2,23),(35,None,23)]:
        review_db.execute(text('INSERT INTO models(id,name,org_id,print_file_id) VALUES(:id,:name,:org,:file)'),{'id':identifier,'name':'Synthetic geometry','org':org,'file':file})
    review_db.commit();return client,issue

@pytest.mark.parametrize('path',['/print-files/22/mesh','/models/32/mesh','/models/33/mesh','/models/34/mesh'])
def test_foreign_mesh_or_link_denied(preview_client,path):
    client,issue=preview_client
    response=client.get(path,headers=issue(['read:models']))
    assert response.status_code==404
    assert 'vertices' not in response.text

@pytest.mark.parametrize('path',['/print-files/21/mesh','/models/31/mesh','/files/21/preview-model'])
def test_unrelated_token_scope_cannot_preview(preview_client,path):
    client,issue=preview_client
    assert client.get(path,headers=issue(['read:printers'])).status_code==403

def test_foreign_preview_denied_before_filesystem_access(preview_client,monkeypatch):
    from modules.archives.routes import archives_crud
    client,issue=preview_client;consulted=[]
    original=archives_crud.os.path.exists
    def observe(path):
        if path=='/opaque/synthetic-22.3mf':consulted.append(path)
        return original(path)
    monkeypatch.setattr(archives_crud.os.path,'exists',observe)
    assert client.get('/files/22/preview-model',headers=issue(['read:models'])).status_code==404
    assert consulted==[]

@pytest.mark.parametrize('path',['/print-files/11/mesh','/models/12/mesh','/files/11/preview-model'])
def test_other_student_same_org_submission_geometry_is_private(preview_client,review_db,path):
    client,issue=preview_client
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    response=client.get(path,headers=issue(['read:models']))
    assert response.status_code==404
    assert 'vertices' not in response.text

@pytest.mark.parametrize('path',['/print-files/21/mesh','/models/31/mesh','/print-files/23/mesh','/models/35/mesh'])
@pytest.mark.parametrize('scopes',[['read:models'],['read'],['admin']])
def test_owned_and_global_geometry_reads_preserved(preview_client,path,scopes):
    client,issue=preview_client
    response=client.get(path,headers=issue(scopes))
    assert response.status_code==200 and response.json()=={'vertices':[0,0,0,1,0,0,0,1,0],'faces':[0,1,2]}

def test_other_student_preview_denied_before_filesystem_access(preview_client,review_db,monkeypatch):
    from modules.archives.routes import archives_crud
    client,issue=preview_client;consulted=[]
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    original=archives_crud.os.path.exists
    def observe(path):
        if path=='/opaque/upload-1.3mf':consulted.append(path)
        return original(path)
    monkeypatch.setattr(archives_crud.os.path,'exists',observe)
    assert client.get('/files/11/preview-model',headers=issue(['read:models'])).status_code==404
    assert consulted==[]

def principal_headers(issue,db,user_id,scopes=None):
    headers=issue(scopes or ['read:models'])
    db.execute(text('UPDATE api_tokens SET user_id=:uid WHERE id=(SELECT MAX(id) FROM api_tokens)'),{'uid':user_id});db.commit()
    return headers

@pytest.mark.parametrize('user_id',[1,2,3])
@pytest.mark.parametrize('path',['/print-files/11/mesh','/models/12/mesh'])
def test_education_owner_manager_and_admin_can_read_mesh(preview_client,review_db,user_id,path):
    client,issue=preview_client
    response=client.get(path,headers=principal_headers(issue,review_db,user_id))
    assert response.status_code==200 and response.json()['faces']==[0,1,2]

@pytest.mark.parametrize('change',['revoked','inactive_center','inconsistent_job'])
@pytest.mark.parametrize('path',['/print-files/11/mesh','/models/12/mesh'])
def test_education_authority_changes_deny_mesh(preview_client,review_db,change,path):
    client,issue=preview_client
    if change=='revoked':review_db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"))
    elif change=='inactive_center':review_db.execute(text("UPDATE education_cost_centers SET state='archived' WHERE id=7"))
    else:
        # Model a legacy corrupt graph in this disposable database only.
        review_db.commit();review_db.execute(text('PRAGMA foreign_keys=OFF'))
        review_db.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=13'))
        review_db.commit();review_db.execute(text('PRAGMA foreign_keys=ON'))
    review_db.commit()
    assert client.get(path,headers=principal_headers(issue,review_db,2)).status_code==404

@pytest.mark.parametrize('provider',['missing','older','failing'])
def test_education_file_requires_available_authority(preview_client,review_db,monkeypatch,provider):
    from core.registry import registry
    client,issue=preview_client
    class Failing:
        def assert_file_preview_allowed(self,*args,**kwargs):raise RuntimeError('Synthetic policy failure')
    if provider=='missing':monkeypatch.delitem(registry._providers,'EducationPolicyProvider',raising=False)
    else:monkeypatch.setitem(registry._providers,'EducationPolicyProvider',Failing() if provider=='failing' else object())
    assert client.get('/print-files/11/mesh',headers=principal_headers(issue,review_db,1)).status_code==404
    assert client.get('/print-files/21/mesh',headers=issue(['read:models'])).status_code==200

@pytest.mark.parametrize('provider_present',[False,True])
def test_operation_only_file_ownership_cannot_fall_through_as_generic(preview_client,review_db,monkeypatch,provider_present):
    from core.registry import registry
    client,issue=preview_client
    review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/synthetic-21.3mf' WHERE operation_id='upload-1'"));review_db.commit()
    if not provider_present:monkeypatch.delitem(registry._providers,'EducationPolicyProvider',raising=False)
    assert client.get('/print-files/21/mesh',headers=principal_headers(issue,review_db,1)).status_code==404

@pytest.mark.parametrize('path',['/print-files/11/mesh','/models/12/mesh'])
def test_superadmin_does_not_invent_education_student_access(preview_client,review_db,path):
    client,issue=preview_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
    assert client.get(path,headers=issue(['admin'])).status_code==404

@pytest.mark.parametrize('user_id',[1,2,3])
def test_authorized_education_preview_serves_actual_file_bytes(preview_client,review_db,monkeypatch,tmp_path,user_id):
    from modules.archives.routes import archives_crud
    client,issue=preview_client;path=tmp_path/'gear.3mf';path.write_bytes(b'synthetic sliced model bytes')
    monkeypatch.setattr(archives_crud,'PREVIEW_STORAGE_ROOT',str(tmp_path))
    review_db.execute(text('UPDATE print_files SET stored_path=:path WHERE id=11'),{'path':str(path)})
    review_db.execute(text("UPDATE education_upload_operations SET final_path=:path WHERE operation_id='upload-1'"),{'path':str(path)})
    review_db.commit()
    response=client.get('/files/11/preview-model',headers=principal_headers(issue,review_db,user_id))
    assert response.status_code==200 and response.content==b'synthetic sliced model bytes'
    assert response.headers['content-type']=='application/vnd.ms-package.3dmanufacturing-3dmodel+xml'
    assert 'gear.3mf' in response.headers['content-disposition']

@pytest.mark.parametrize('escape',['direct','symlink','prefix_sibling'])
def test_authorized_preview_rejects_path_escape_before_exists(preview_client,review_db,monkeypatch,tmp_path,escape):
    from modules.archives.routes import archives_crud
    client,issue=preview_client;root=tmp_path/'owned';root.mkdir();outside=tmp_path/'owned-other';outside.mkdir();foreign=outside/'file.3mf';foreign.write_bytes(b'synthetic outside file')
    link=root/'link.3mf';link.symlink_to(foreign)
    path=link if escape=='symlink' else foreign if escape=='prefix_sibling' else tmp_path/'file.3mf'
    monkeypatch.setattr(archives_crud,'PREVIEW_STORAGE_ROOT',str(root))
    review_db.execute(text('UPDATE print_files SET stored_path=:path WHERE id=21'),{'path':str(path)});review_db.commit()
    original=archives_crud.os.path.exists;checked=[]
    def observe(value):
        checked.append(value)
        return original(value)
    monkeypatch.setattr(archives_crud.os.path,'exists',observe)
    response=client.get('/files/21/preview-model',headers=issue(['read:models']))
    assert response.status_code==400
    assert str(foreign) not in checked and str(path) not in checked

def test_corrupt_mesh_json_has_controlled_no_data_response(preview_client,review_db):
    client,issue=preview_client
    review_db.execute(text("UPDATE print_files SET mesh_data='not JSON' WHERE id=21"));review_db.commit()
    response=client.get('/print-files/21/mesh',headers=issue(['read:models']))
    assert response.status_code==404 and response.json()=={'detail':'Preview not found'}

@pytest.mark.parametrize('change',[
    "UPDATE education_upload_operations SET user_id=4 WHERE operation_id='upload-1'",
    "UPDATE education_upload_operations SET org_id=2 WHERE operation_id='upload-1'",
    "UPDATE education_upload_operations SET final_path='/opaque/different.3mf' WHERE operation_id='upload-1'",
    "UPDATE education_upload_operations SET state='needs_reconciliation' WHERE operation_id='upload-1'",
    'UPDATE print_files SET created_by=4 WHERE id=11',
    'UPDATE models SET print_file_id=21 WHERE id=12',
    'UPDATE models SET org_id=2 WHERE id=12',
    'UPDATE jobs SET model_id=31 WHERE id=13',
])
@pytest.mark.parametrize('user_id',[1,2,3])
def test_corrupt_education_file_graph_denies_every_audience(preview_client,review_db,change,user_id):
    client,issue=preview_client
    # Deliberately bypass constraints only in this isolated synthetic SQLite DB.
    review_db.commit();review_db.execute(text('PRAGMA foreign_keys=OFF'))
    review_db.execute(text(change));review_db.commit();review_db.execute(text('PRAGMA foreign_keys=ON'))
    response=client.get('/print-files/11/mesh',headers=principal_headers(issue,review_db,user_id))
    assert response.status_code==404 and 'vertices' not in response.text
