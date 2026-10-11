"""Real HTTP archive ownership checks; identity projection is substituted.

Printer sharing grants device access, not another tenant's private archive.
These isolated legacy rows have authoritative job/file ownership.
"""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def private_archive(reprint_client,review_db):
 client,identity,_=reprint_client
 review_db.execute(text("UPDATE print_files SET org_id=1,thumbnail_b64='synthetic-private-thumbnail' WHERE id=101"))
 review_db.execute(text("UPDATE print_archives SET print_name='Tenant A private fixture',thumbnail_b64='synthetic-private-thumbnail',tags='private-fixture' WHERE id=101"))
 review_db.commit()
 identity.update(id=5,group_id=2,role='admin')
 return client,review_db

def transition(db,state):
 if state=='shared':db.execute(text('UPDATE printers SET shared=1 WHERE id=9'))
 elif state=='reassigned':
  # Remove the isolated fixture's tenant-bound device grant before a valid
  # reassignment. Keep the original private job/file attribution intact.
  db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
  db.execute(text('UPDATE printers SET org_id=2 WHERE id=9'))
 else:
  # Preserve a historical archive after printer removal without destroying
  # the private source records; FK schema sets its printer reference NULL.
  db.execute(text('UPDATE print_archives SET printer_id=NULL WHERE id=101'))
 db.commit()

@pytest.mark.parametrize('state',['shared','reassigned','removed'])
def test_foreign_private_archive_detail_denied_despite_device_transition(private_archive,state):
 client,db=private_archive;transition(db,state)
 response=client.get('/archives/101')
 assert response.status_code==404
 assert 'synthetic-private-thumbnail' not in response.text

@pytest.mark.parametrize('state',['shared','reassigned','removed'])
@pytest.mark.parametrize('endpoint',['/archives','/archives/log','/archives/log/export'])
def test_foreign_private_archive_not_listed_or_exported(private_archive,state,endpoint):
 client,db=private_archive;transition(db,state)
 response=client.get(endpoint)
 assert response.status_code==200
 assert 'Tenant A private fixture' not in response.text
 assert 'synthetic-private-thumbnail' not in response.text

@pytest.mark.parametrize('state',['shared','reassigned','removed'])
@pytest.mark.parametrize('action',['notes','tags','delete'])
def test_foreign_private_archive_mutation_denied_without_partial_change(private_archive,state,action):
 client,db=private_archive;transition(db,state)
 before=tuple(db.execute(text('SELECT id,print_name,notes,tags FROM print_archives WHERE id=101')).one())
 if action=='notes':response=client.patch('/archives/101',json={'notes':'foreign modification'})
 elif action=='tags':response=client.patch('/archives/101/tags',json={'tags':['foreign-modification']})
 else:response=client.delete('/archives/101')
 assert response.status_code==404
 db.expire_all()
 assert tuple(db.execute(text('SELECT id,print_name,notes,tags FROM print_archives WHERE id=101')).one())==before


@pytest.mark.parametrize('state',['shared','reassigned','removed'])
def test_original_owner_retains_known_archive_after_device_transition(private_archive,reprint_client,state):
 client,db=private_archive;transition(db,state)
 _,identity,_=reprint_client;identity.update(id=3,group_id=1,role='admin')
 response=client.get('/archives/101')
 assert response.status_code==200,response.text
 assert response.json()['thumbnail_b64']=='synthetic-private-thumbnail'
 assert client.get('/archives').json()['total']==1


@pytest.mark.parametrize('role,org,expected',[('admin',1,404),('viewer',None,404),('admin',None,200)])
def test_unknown_archive_is_only_visible_for_superadmin_reconciliation(private_archive,reprint_client,role,org,expected):
 client,db=private_archive
 db.execute(text("UPDATE print_archives SET ownership_state=NULL,org_id=NULL WHERE id=101"));db.commit()
 _,identity,_=reprint_client;identity.update(id=3,role=role,group_id=org)
 assert client.get('/archives/101').status_code==expected
 assert client.get('/archives').json()['total']==(1 if expected==200 else 0)


@pytest.mark.parametrize('endpoint,method,scopes,expected',[
 ('/archives/101','get',['read:printers'],403),('/archives','get',['write:jobs'],403),
 ('/archives/101','patch',['read:jobs'],403),('/archives/101/tags','patch',['read:jobs'],403),
 ('/archives/101','delete',['read:jobs'],403),('/archives/101','get',['read:jobs'],200),
])
def test_archive_scoped_token_enforces_resource_and_action(private_archive,reprint_client,endpoint,method,scopes,expected):
 client,db=private_archive;_,identity,_=reprint_client
 identity.update(id=3,role='admin',group_id=1,_auth_kind='user_api_token',_token_scopes=scopes)
 options={'json':{'notes':'scoped','tags':['scoped']}} if method=='patch' else {}
 assert getattr(client,method)(endpoint,**options).status_code==expected


@pytest.mark.parametrize('user,role,org,expected',[(1,'viewer',1,200),(2,'viewer',1,200),(3,'admin',1,200),(4,'viewer',1,404),(5,'admin',2,404)])
def test_education_archive_uses_real_submission_audience(private_archive,reprint_client,user,role,org,expected):
 client,db=private_archive;_,identity,_=reprint_client
 db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
 db.execute(text("UPDATE print_archives SET job_id=13,print_file_id=11,education_submission_id=14 WHERE id=101"));db.commit()
 identity.update(id=user,role=role,group_id=org)
 response=client.get('/archives/101')
 assert response.status_code==expected,response.text
 assert client.get('/archives?per_page=1').json()['total']==(1 if expected==200 else 0)
 assert ('Tenant A private fixture' in client.get('/archives/log/export').text)==(expected==200)


def test_education_marker_survives_missing_source_without_becoming_generic(private_archive,reprint_client):
 client,db=private_archive;_,identity,_=reprint_client
 db.execute(text("UPDATE print_archives SET education_submission_id=9999 WHERE id=101"));db.commit()
 identity.update(id=3,role='admin',group_id=1)
 assert client.get('/archives/101').status_code==404
 assert client.get('/archives').json()['total']==0


def test_known_global_orphan_remains_readable(private_archive,reprint_client):
 client,db=private_archive
 db.execute(text("UPDATE print_archives SET org_id=NULL,printer_id=NULL WHERE id=101"));db.commit()
 assert client.get('/archives/101').status_code==200
 assert client.get('/archives').json()['total']==1


@pytest.mark.parametrize('operation',['list','rename','delete'])
def test_global_tag_operations_preserve_foreign_private_tags(private_archive,operation):
 from modules.archives.routes import tags
 client,db=private_archive;client.app.include_router(tags.router)
 if operation=='list':
  response=client.get('/tags');assert response.status_code==200
  assert response.json()['tags']==[]
 elif operation=='rename':
  response=client.post('/tags/rename',json={'old':'private-fixture','new':'foreign-edit'})
  assert response.status_code==200 and response.json()['updated']==0
 else:
  response=client.delete('/tags/private-fixture')
  assert response.status_code==200 and response.json()['updated']==0
 db.expire_all()
 assert db.execute(text('SELECT tags FROM print_archives WHERE id=101')).scalar_one()=='private-fixture'


def test_mixed_project_assignment_rejects_all_changes(private_archive):
 from modules.archives.routes import projects
 client,db=private_archive;client.app.include_router(projects.router)
 db.execute(text("INSERT INTO projects(id,name,org_id) VALUES(101,'Foreign user own project',2)"))
 db.execute(text("INSERT INTO print_archives(id,org_id,ownership_state,ownership_source,print_name,status) VALUES(102,2,'known','synthetic_owner','Own archive','completed')"));db.commit()
 response=client.post('/projects/101/archives',json={'archive_ids':[101,102]})
 assert response.status_code==404,response.text
 db.expire_all()
 assert db.execute(text('SELECT COUNT(*) FROM print_archives WHERE project_id IS NOT NULL')).scalar_one()==0


def test_global_project_counts_and_zip_exclude_foreign_history(private_archive):
 import io,zipfile,json
 from modules.archives.routes import projects
 client,db=private_archive;client.app.include_router(projects.router)
 db.execute(text("INSERT INTO projects(id,name,org_id) VALUES(101,'Global project',NULL)"))
 db.execute(text('UPDATE print_archives SET project_id=101 WHERE id=101'));db.commit()
 detail=client.get('/projects/101');assert detail.status_code==200,detail.text
 assert detail.json()['archives']==[] and detail.json()['archive_count']==0
 assert client.get('/projects').json()[0]['archive_count']==0
 response=client.get('/projects/101/export');assert response.status_code==200,response.text
 with zipfile.ZipFile(io.BytesIO(response.content)) as package:
  assert len(package.namelist())==1
  assert 'Tenant A private fixture' not in package.read(package.namelist()[0]).decode()


def test_missing_education_authority_denies_marked_history(private_archive,reprint_client,monkeypatch):
 from core.registry import registry
 client,db=private_archive;_,identity,_=reprint_client
 db.execute(text('UPDATE print_archives SET education_submission_id=14 WHERE id=101'));db.commit()
 identity.update(id=3,role='admin',group_id=1)
 monkeypatch.setattr(registry,'get_provider',lambda name:None)
 assert client.get('/archives/101').status_code==404
 assert client.get('/archives').json()['total']==0


@pytest.mark.parametrize('source',['foreign','education','operation_only'])
def test_ams_preview_authorizes_original_file_independently(private_archive,reprint_client,source):
 import json
 client,db=private_archive;_,identity,original=reprint_client
 identity.update(id=4,role='viewer',group_id=1)
 if source=='foreign':
  db.execute(text("UPDATE print_files SET org_id=2,filaments_json=:materials WHERE id=101"),{'materials':json.dumps([{'color':'private-material-marker','type':'PLA'}])})
 elif source=='education':
  db.execute(text('UPDATE print_archives SET print_file_id=11 WHERE id=101'))
  db.execute(text("UPDATE print_files SET filaments_json=:materials WHERE id=11"),{'materials':json.dumps([{'color':'private-material-marker','type':'PLA'}])})
  db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
 else:
  db.execute(text('UPDATE education_upload_operations SET final_path=:path WHERE operation_id=\'upload-1\''),{'path':str(original)})
  db.execute(text("UPDATE print_files SET filaments_json=:materials WHERE id=101"),{'materials':json.dumps([{'color':'private-material-marker','type':'PLA'}])})
 db.commit()
 response=client.get('/archives/101/ams-preview')
 assert response.status_code==404,response.text
 assert 'private-material-marker' not in response.text


def test_operation_only_education_source_does_not_become_generic_archive(archive_case):
 # Imported fixture below exercises real capture and archival, not forged anchors.
 archive,engine=archive_case
 with engine.begin() as conn:
  conn.execute(text("UPDATE education_upload_operations SET final_path='/opaque/archive-synthetic.3mf' WHERE operation_id='upload-1'"))
  conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
  from modules.archives.attempt_accounting import capture_attempt_spools
  assert capture_attempt_spools(conn,7099,9,source='synthetic-operation-owner')
  conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
 assert archive.create_print_archive(7099,9,True) is not None
 with engine.connect() as conn:
  owner=conn.execute(text('SELECT ownership_state,education_submission_id FROM print_archives WHERE print_job_id=7099')).one()
  assert owner.ownership_state=='unresolved'
  assert owner.education_submission_id is not None

from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case


def test_missing_authority_does_not_expose_unmarked_operation_owned_file(private_archive,reprint_client,monkeypatch):
 from core.registry import registry
 client,db=private_archive;_,identity,original=reprint_client
 identity.update(id=4,role='viewer',group_id=1)
 db.execute(text("UPDATE education_upload_operations SET final_path=:path WHERE operation_id='upload-1'"),{'path':str(original)});db.commit()
 monkeypatch.setattr(registry,'get_provider',lambda name:None)
 assert client.get('/archives/101').status_code==404
 assert client.get('/archives').json()['total']==0


def test_duplicate_education_file_reference_is_rejected_by_schema(private_archive):
 from sqlalchemy.exc import IntegrityError
 client,db=private_archive
 db.execute(text("INSERT INTO jobs(id,item_name,model_id,submitted_by,charged_to_org_id) VALUES(15,'Duplicate file source',12,1,1)"))
 db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes) VALUES('synthetic-duplicate-op',1,1,'committed',0,0,0)"));db.commit()
 with pytest.raises(IntegrityError,match='print_file_id'):
  db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision) SELECT 15,org_id,'synthetic-duplicate-op',15,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision FROM education_submissions WHERE id=14"))
 db.rollback()


def test_extra_upload_operation_cannot_expand_education_archive_audience(private_archive,reprint_client):
 client,db=private_archive;_,identity,_=reprint_client
 identity.update(id=1,role='viewer',group_id=1)
 db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
 db.execute(text("UPDATE print_archives SET job_id=13,print_file_id=11,education_submission_id=14 WHERE id=101"))
 db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,final_path) VALUES('synthetic-other-op',1,4,'committed',0,0,0,'/opaque/upload-1.3mf')"));db.commit()
 assert client.get('/archives/101').status_code==404
 assert client.get('/archives').json()['total']==0


def test_ams_preview_uses_the_authorized_file_snapshot_during_source_change(private_archive,reprint_client,monkeypatch):
 import json
 from core import model_preview_access
 client,db=private_archive;_,identity,_=reprint_client
 identity.update(id=3,role='operator',group_id=1)
 db.execute(text('UPDATE print_files SET filaments_json=:materials WHERE id=101'),{'materials':json.dumps([{'color':'original-authorized','type':'PLA'}])});db.commit()
 original=model_preview_access.accessible_preview_file
 def intervening_change(request_db,file_id,principal):
  authorized=original(request_db,file_id,principal)
  request_db.execute(text('UPDATE print_files SET org_id=2,filaments_json=:materials WHERE id=101'),{'materials':json.dumps([{'color':'foreign-after-auth','type':'PLA'}])})
  request_db.commit()
  return authorized
 monkeypatch.setattr(model_preview_access,'accessible_preview_file',intervening_change)
 response=client.get('/archives/101/ams-preview')
 assert response.status_code==200,response.text
 assert response.json()['required'][0]['color']=='original-authorized'
 assert 'foreign-after-auth' not in response.text
