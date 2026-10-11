"""Actual project HTTP/auth/database assertions with synthetic isolated records."""
import io,json,zipfile
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def project_client(token_client,review_db):
    from modules.archives.routes import projects
    client,issue=token_client;client.app.include_router(projects.router)
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,is_active) VALUES(19,'Foreign synthetic','bambu',2,1)"))
    for identifier,org,name in [(101,1,'Own project'),(102,2,'Foreign project')]:
        review_db.execute(text('INSERT INTO projects(id,name,org_id,created_by) VALUES(:id,:name,:org,3)'),{'id':identifier,'name':name,'org':org})
    for identifier,printer,name in [(201,9,'Own archive'),(202,19,'Foreign archive')]:
        review_db.execute(text("INSERT INTO print_archives(id,printer_id,print_name,status) VALUES(:id,:printer,:name,'completed')"),{'id':identifier,'printer':printer,'name':name})
    review_db.execute(text("UPDATE print_archives SET org_id=CASE id WHEN 201 THEN 1 ELSE 2 END,ownership_state='known',ownership_source='synthetic_owner'"))
    review_db.commit();return client,issue

def zip_metadata():
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:archive.writestr('project.json',json.dumps({'name':'Synthetic import'}))
    return out.getvalue()

@pytest.mark.parametrize('action',['create','update','delete','assign','import'])
def test_read_token_cannot_mutate_project(project_client,review_db,action):
    client,issue=project_client;headers=issue(['read:jobs'])
    if action=='create':response=client.post('/projects',headers=headers,json={'name':'Denied create'})
    elif action=='update':response=client.put('/projects/101',headers=headers,json={'name':'Denied rename'})
    elif action=='delete':response=client.delete('/projects/101',headers=headers)
    elif action=='assign':response=client.post('/projects/101/archives',headers=headers,json={'archive_ids':[201]})
    else:response=client.post('/projects/import',headers=headers,files={'file':('synthetic.zip',zip_metadata(),'application/zip')})
    assert response.status_code==403
    assert review_db.execute(text('SELECT COUNT(*) FROM projects')).scalar_one()==2
    assert review_db.execute(text('SELECT name FROM projects WHERE id=101')).scalar_one()=='Own project'
    assert review_db.execute(text('SELECT project_id FROM print_archives WHERE id=201')).scalar_one() is None

@pytest.mark.parametrize('path',['/projects','/projects/101','/projects/101/export'])
def test_unrelated_scope_cannot_read_projects(project_client,path):
    client,issue=project_client
    assert client.get(path,headers=issue(['write:printers'])).status_code==403

def test_foreign_org_project_create_denied_before_insert(project_client,review_db):
    client,issue=project_client
    response=client.post('/projects',headers=issue(['write:jobs']),json={'name':'Foreign denied','org_id':2})
    assert response.status_code==404
    assert review_db.execute(text('SELECT COUNT(*) FROM projects')).scalar_one()==2

def test_mixed_foreign_archive_assignment_is_atomic(project_client,review_db):
    client,issue=project_client
    response=client.post('/projects/101/archives',headers=issue(['write:jobs']),json={'archive_ids':[201,202]})
    assert response.status_code==404
    assert review_db.execute(text('SELECT COUNT(*) FROM print_archives WHERE project_id IS NOT NULL')).scalar_one()==0

@pytest.mark.parametrize('action',['detail','export'])
def test_legacy_foreign_assignment_does_not_disclose_archive(project_client,review_db,action):
    client,issue=project_client
    review_db.execute(text('UPDATE print_archives SET project_id=101'));review_db.commit()
    response=client.get('/projects/101'+('/export' if action=='export' else ''),headers=issue(['read:jobs']))
    assert response.status_code==200
    data=json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read('project.json')) if action=='export' else response.json()
    assert [a['id'] for a in data['archives']]==[201]

def test_owned_archive_assignment_persists(project_client,review_db):
    client,issue=project_client
    response=client.post('/projects/101/archives',headers=issue(['write:jobs']),json={'archive_ids':[201]})
    assert response.status_code==200
    assert review_db.execute(text('SELECT project_id FROM print_archives WHERE id=201')).scalar_one()==101

def test_legacy_foreign_assignment_does_not_leak_list_count(project_client,review_db):
    client,issue=project_client
    review_db.execute(text('UPDATE print_archives SET project_id=101'));review_db.commit()
    response=client.get('/projects',headers=issue(['read:jobs']))
    assert response.status_code==200
    own=next(p for p in response.json() if p['id']==101)
    assert own['archive_count']==1

@pytest.mark.parametrize('archives',[[201,999],[201,202,999]])
def test_mixed_missing_archive_assignment_leaves_batch_unchanged(project_client,review_db,archives):
    client,issue=project_client
    response=client.post('/projects/101/archives',headers=issue(['write:jobs']),json={'archive_ids':archives})
    assert response.status_code==404
    assert review_db.execute(text('SELECT COUNT(*) FROM print_archives WHERE project_id IS NOT NULL')).scalar_one()==0

def test_duplicate_assignment_reports_unique_persisted_archive_count(project_client,review_db):
    client,issue=project_client
    response=client.post('/projects/101/archives',headers=issue(['write:jobs']),json={'archive_ids':[201,201]})
    assert response.status_code==200 and response.json()['assigned']==1
    assert review_db.execute(text('SELECT COUNT(*) FROM print_archives WHERE project_id=101')).scalar_one()==1

@pytest.mark.parametrize('scopes',[['read'],['read:jobs'],['admin'],['read','write'],['read:jobs','write:jobs']])
def test_authorized_project_reads_preserve_scope_policy(project_client,scopes):
    client,issue=project_client;headers=issue(scopes)
    assert [p['id'] for p in client.get('/projects',headers=headers).json()]==[101]
    assert client.get('/projects/101',headers=headers).status_code==200
    assert client.get('/projects/101/export',headers=headers).status_code==200
    assert client.get('/projects/102',headers=headers).status_code==404

@pytest.mark.parametrize('scopes',[['write'],['write:jobs'],['admin']])
def test_authorized_create_defaults_to_owner_org(project_client,review_db,scopes):
    client,issue=project_client
    response=client.post('/projects',headers=issue(scopes),json={'name':'Created owned','expected_parts':0})
    assert response.status_code==200
    row=review_db.execute(text('SELECT name,org_id,expected_parts FROM projects WHERE id=:id'),{'id':response.json()['id']}).one()
    assert tuple(row)==('Created owned',1,0)

@pytest.mark.parametrize('role,export_code,delete_code',[('viewer',403,403),('operator',200,403),('admin',200,200)])
def test_project_role_floors_preserved(project_client,review_db,role,export_code,delete_code):
    client,issue=project_client
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'),{'role':role});review_db.commit()
    headers=issue(['read:jobs','write:jobs'])
    assert client.get('/projects/101',headers=headers).status_code==200
    assert client.get('/projects/101/export',headers=headers).status_code==export_code
    assert client.delete('/projects/101',headers=headers).status_code==delete_code

@pytest.mark.parametrize('scopes',[['write'],['write:jobs']])
def test_write_permission_alone_does_not_grant_project_reads(project_client,scopes):
    client,issue=project_client
    assert client.get('/projects/101',headers=issue(scopes)).status_code==403

def test_shared_foreign_printer_does_not_expose_private_archive(project_client,review_db):
    client,issue=project_client
    review_db.execute(text('UPDATE printers SET shared=TRUE WHERE id=19'))
    review_db.execute(text('UPDATE print_archives SET project_id=101'));review_db.commit()
    headers=issue(['read:jobs','write:jobs'])
    data=client.get('/projects/101',headers=headers).json()
    assert {a['id'] for a in data['archives']}=={201}
    assert next(p for p in client.get('/projects',headers=headers).json() if p['id']==101)['archive_count']==1
    review_db.execute(text('UPDATE print_archives SET project_id=NULL'));review_db.commit()
    assert client.post('/projects/101/archives',headers=headers,json={'archive_ids':[202]}).status_code==404
    assert review_db.execute(text('SELECT project_id FROM print_archives WHERE id=202')).scalar_one() is None

def test_superadmin_can_assign_foreign_archive(project_client,review_db):
    client,issue=project_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
    headers=issue(['admin'])
    response=client.post('/projects/101/archives',headers=headers,json={'archive_ids':[202]})
    assert response.status_code==200 and response.json()['assigned']==1
    assert [a['id'] for a in client.get('/projects/101',headers=headers).json()['archives']]==[202]
    assert client.post('/projects',headers=headers,json={'name':'Authorized foreign','org_id':2}).status_code==200

def test_interactive_project_edit_and_soft_delete_persist(project_client,review_db):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,_=project_client;headers=interactive_headers()
    assert client.put('/projects/101',headers=headers,json={'name':'Renamed owned','expected_parts':0}).status_code==200
    assert tuple(review_db.execute(text('SELECT name,expected_parts FROM projects WHERE id=101')).one())==('Renamed owned',0)
    assert client.delete('/projects/101',headers=headers).status_code==200
    assert review_db.execute(text('SELECT status FROM projects WHERE id=101')).scalar_one()=='archived'
    assert client.get('/projects?status=active',headers=headers).json()==[]

@pytest.mark.parametrize('parent',['null','unscoped','missing'])
def test_private_archive_ownership_survives_missing_or_global_printer(project_client,review_db,parent):
    client,issue=project_client
    if parent=='unscoped':
        review_db.execute(text('UPDATE printers SET org_id=NULL WHERE id=19'));printer=19
    elif parent=='null':printer=None
    else:
        review_db.commit();review_db.execute(text('PRAGMA foreign_keys=OFF'));printer=999
    review_db.execute(text('UPDATE print_archives SET printer_id=:printer,project_id=101 WHERE id=202'),{'printer':printer});review_db.commit()
    headers=issue(['read:jobs','write:jobs'])
    archives=client.get('/projects/101',headers=headers).json()['archives']
    assert [a['id'] for a in archives]==[]
    response=client.post('/projects/101/archives',headers=headers,json={'archive_ids':[202]})
    assert response.status_code==404

def test_export_excludes_foreign_file_bytes_before_filesystem_access(project_client,review_db,tmp_path,monkeypatch):
    from modules.archives.routes import projects
    client,issue=project_client
    own=tmp_path/'own.3mf';foreign=tmp_path/'foreign.3mf'
    own.write_bytes(b'synthetic own file');foreign.write_bytes(b'synthetic foreign file')
    for identifier,path in [(201,own),(202,foreign)]:
        review_db.execute(text('UPDATE print_archives SET project_id=101,file_path=:path WHERE id=:id'),{'path':str(path),'id':identifier})
    review_db.commit()
    original_exists=projects.os.path.exists;checked=[]
    def observed_exists(path):
        checked.append(path)
        assert path!=str(foreign), 'Foreign file consulted before authorization'
        return original_exists(path)
    monkeypatch.setattr(projects.os.path,'exists',observed_exists)
    response=client.get('/projects/101/export',headers=issue(['read:jobs']))
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist())=={'project.json','files/own.3mf'}
        assert archive.read('files/own.3mf')==b'synthetic own file'
        assert [a['id'] for a in json.loads(archive.read('project.json'))['archives']]==[201]
    assert str(own) in checked and str(foreign) not in checked
