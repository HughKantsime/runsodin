"""Actual generic upload HTTP/DB and configured owned filesystem roots."""
import hashlib
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def upload_client(token_client,tmp_path,monkeypatch):
 import shutil
 from modules.models_library.routes import print_files
 from modules.models_library import print_file_meta
 client,issue=token_client;client.app.include_router(print_files.router)
 print_files.limiter._storage.reset()
 storage=tmp_path/'owned-upload-storage';storage.mkdir()
 monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(storage))
 return TestClient(client.app,raise_server_exceptions=False),issue,storage

GCODE=b'; Synthetic safe metadata fixture\n; printer_model = MK4\nG28\n'

def upload(client,headers,filename,content):
 return client.post('/print-files/upload',headers=headers,files={'file':(filename,content,'application/octet-stream')})

@pytest.mark.parametrize('extension',['.gcode','.bgcode','.3mf'])
def test_upload_actor_ownership_and_exact_hash_persist_for_all_formats(upload_client,review_db,extension):
 client,issue,storage=upload_client
 content=Path('tests/fixtures/release_gate/candidate-calibration-cube.3mf').read_bytes() if extension=='.3mf' else GCODE
 response=upload(client,issue(['write:models']),'Synthetic owned'+extension,content)
 assert response.status_code==200,response.text
 body=response.json()
 file=review_db.execute(text('SELECT org_id,created_by,file_hash FROM print_files WHERE id=:id'),{'id':body['id']}).one()
 assert tuple(file)==(1,3,hashlib.sha256(content).hexdigest())
 assert review_db.execute(text('SELECT org_id FROM models WHERE id=:id'),{'id':body['model_id']}).scalar_one()==1
 assert any(path.read_bytes()==content for path in storage.rglob('*') if path.is_file())

@pytest.mark.parametrize('existing_org,should_warn',[(1,True),(2,False)])
def test_duplicate_response_never_reveals_foreign_file(upload_client,review_db,existing_org,should_warn):
 client,issue,storage=upload_client
 review_db.execute(text("INSERT INTO print_files(id,filename,org_id,file_hash) VALUES(201,'Synthetic private duplicate',:org,:hash)"),{'org':existing_org,'hash':hashlib.sha256(GCODE).hexdigest()});review_db.commit()
 response=upload(client,issue(['write:models']),'Synthetic duplicate.gcode',GCODE)
 assert response.status_code==200,response.text
 duplicate=response.json()['duplicate']
 if should_warn:assert duplicate=={'duplicate':True,'existing_file_id':201,'existing_file_name':'Synthetic private duplicate'}
 else:assert duplicate is None and 'Synthetic private duplicate' not in response.text

@pytest.mark.parametrize('existing_org,attach',[(1,True),(2,False),(None,False)])
def test_same_name_upload_only_attaches_to_actors_own_model(upload_client,review_db,existing_org,attach):
 client,issue,storage=upload_client
 review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(201,'Synthetic same name',:org)"),{'org':existing_org});review_db.commit()
 response=upload(client,issue(['write:models']),'Synthetic same name.gcode',GCODE)
 assert response.status_code==200,response.text
 assert (response.json()['model_id']==201) is attach
 assert review_db.execute(text('SELECT org_id FROM models WHERE id=:id'),{'id':response.json()['model_id']}).scalar_one()==1

@pytest.mark.parametrize('scopes',[['read:models'],['write:jobs'],['read:printers']])
def test_read_or_unrelated_upload_tokens_leave_database_and_storage_unchanged(upload_client,review_db,scopes):
 client,issue,storage=upload_client
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()
 response=upload(client,issue(scopes),'Synthetic denied.gcode',GCODE)
 assert response.status_code==403
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()==before
 assert not [p for p in storage.rglob('*') if p.is_file()]


def test_upload_copy_failure_never_persists_partial_file_or_model(upload_client,review_db,monkeypatch):
 import shutil
 client,issue,storage=upload_client
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one();models=review_db.execute(text('SELECT COUNT(*) FROM models')).scalar_one()
 monkeypatch.setattr(shutil,'copyfileobj',lambda *args,**kwargs: (_ for _ in ()).throw(OSError('Synthetic copy refusal')))
 response=upload(client,issue(['write:models']),'Synthetic refused.gcode',GCODE)
 assert response.status_code==500
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()==before
 assert review_db.execute(text('SELECT COUNT(*) FROM models')).scalar_one()==models


def test_private_education_hash_duplicate_does_not_leak_to_other_student(upload_client,review_db):
 client,issue,storage=upload_client
 review_db.execute(text("UPDATE print_files SET file_hash=:hash WHERE id=11"),{'hash':hashlib.sha256(GCODE).hexdigest()})
 review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
 review_db.execute(text("UPDATE users SET role='operator' WHERE id=4"));review_db.commit()
 headers=issue(['write:models']);review_db.execute(text('UPDATE api_tokens SET user_id=4 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 response=upload(client,headers,'Synthetic unrelated.gcode',GCODE)
 assert response.status_code==200,response.text
 assert response.json()['duplicate'] is None
 assert 'gear.3mf' not in response.text


@pytest.mark.parametrize('protected',['submission','operation_alias'])
def test_same_name_education_model_is_never_implicitly_reused(upload_client,review_db,protected):
 client,issue,storage=upload_client
 if protected=='submission':
  review_db.execute(text("UPDATE models SET name='Synthetic protected' WHERE id=12"))
 else:
  review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(201,'Synthetic protected',1)"))
  review_db.execute(text("INSERT INTO print_files(id,filename,model_id,stored_path,org_id) VALUES(202,'Synthetic operation file',201,'/opaque/synthetic-protected',1)"))
  review_db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('synthetic-protected',1,1,'reserved','/opaque/synthetic-protected')"))
 review_db.commit()
 response=upload(client,issue(['write:models']),'Synthetic protected.gcode',GCODE)
 assert response.status_code==200,response.text
 assert response.json()['model_id'] not in (12,201)
 assert response.json()['is_new_model'] is True


def test_ambiguous_owned_model_names_create_new_model_without_arbitrary_attachment(upload_client,review_db):
 client,issue,storage=upload_client
 review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(201,'Synthetic ambiguous',1),(202,'Synthetic ambiguous',1)"));review_db.commit()
 response=upload(client,issue(['write:models']),'Synthetic ambiguous.gcode',GCODE)
 assert response.status_code==200,response.text
 assert response.json()['model_id'] not in (201,202)
 assert response.json()['is_new_model'] is True


@pytest.mark.parametrize('change',['inactive','role','org','token_deleted','token_scope','token_expired'])
def test_upload_authorization_refresh_after_lock_denies_changed_identity(upload_client,review_db,monkeypatch,change):
 from modules.models_library import upload_storage
 client,issue,storage=upload_client
 review_db.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,group_id) VALUES(6,'synthetic uploader','synthetic-uploader@example.test','fixture','operator',1,1)"));review_db.commit()
 headers=issue(['write:models']);review_db.execute(text('UPDATE api_tokens SET user_id=6 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 lock=upload_storage._lock_upload_workflow;seen=[]
 def changed(db):
  lock(db);seen.append(True)
  statements={'inactive':"UPDATE users SET is_active=0 WHERE id=6",'role':"UPDATE users SET role='viewer' WHERE id=6",'org':"UPDATE users SET group_id=2 WHERE id=6",'token_deleted':'DELETE FROM api_tokens WHERE user_id=6','token_scope':"UPDATE api_tokens SET scopes='[\"read:models\"]' WHERE user_id=6",'token_expired':"UPDATE api_tokens SET expires_at='2001-01-01' WHERE user_id=6"}
  db.execute(text(statements[change]));db.commit()
 monkeypatch.setattr(upload_storage,'_lock_upload_workflow',changed)
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()
 response=upload(client,headers,'Synthetic changed.gcode',GCODE)
 assert response.status_code==403,response.text
 assert seen==[True]
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()==before
 assert not [p for p in storage.rglob('*') if p.is_file()]


@pytest.mark.parametrize('boundary',['model_insert','metadata','audit_flush','partial_copy'])
def test_upload_failure_rolls_back_graph_and_only_new_payload(upload_client,review_db,monkeypatch,boundary):
 from sqlalchemy.orm import Session
 from modules.models_library import print_file_meta
 from modules.models_library.routes import print_files
 from core.models import AuditLog
 import shutil
 client,issue,storage=upload_client;original=storage/'synthetic-existing.gcode';original.write_bytes(b'Synthetic existing original')
 before={table:review_db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in ('models','print_files','audit_logs')}
 if boundary=='model_insert':
  execute=Session.execute
  def refused(self,statement,*args,**kwargs):
   if 'INSERT INTO models' in str(statement):raise RuntimeError('Synthetic model refused')
   return execute(self,statement,*args,**kwargs)
  monkeypatch.setattr(Session,'execute',refused)
 elif boundary=='metadata':
  def refused(*args,**kwargs):raise RuntimeError('Synthetic metadata refused')
  monkeypatch.setattr(print_file_meta,'extract_print_file_meta',refused)
 elif boundary=='audit_flush':
  monkeypatch.setattr(print_files,'log_audit',lambda db,*args,**kwargs:db.add(AuditLog(action=None)))
 else:
  def refused(source,destination,*args,**kwargs):
   destination.write(b'Synthetic partial');raise OSError('Synthetic partial copy refused')
  monkeypatch.setattr(shutil,'copyfileobj',refused)
 response=upload(client,issue(['write:models']),'Synthetic rollback.gcode',GCODE)
 assert response.status_code==500,response.text
 assert all(review_db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one()==count for table,count in before.items())
 assert {p for p in storage.rglob('*') if p.is_file()}=={original}
 assert original.read_bytes()==b'Synthetic existing original'


@pytest.mark.parametrize('committed',[False,True])
def test_uncertain_upload_commit_preserves_payload_without_false_success(upload_client,review_db,monkeypatch,committed):
 from sqlalchemy.orm import Session
 client,issue,storage=upload_client;headers=issue(['write:models']);commit=Session.commit;armed=[]
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()
 def uncertain(self):
  if self.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()>before and not armed:
   armed.append(True)
   if committed:commit(self)
   raise RuntimeError('Synthetic unknown commit acknowledgement')
  return commit(self)
 monkeypatch.setattr(Session,'commit',uncertain)
 response=upload(client,headers,'Synthetic uncertain.gcode',GCODE)
 assert response.status_code==500,response.text
 files=[p for p in storage.rglob('*') if p.is_file()]
 assert len(files)==1 and files[0].read_bytes()==GCODE
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()==before+int(committed)


def test_upload_lock_contention_returns_controlled_retry_without_residue(upload_client,review_db,monkeypatch):
 from sqlalchemy.exc import OperationalError
 from modules.models_library import upload_storage
 client,issue,storage=upload_client
 class Busy(Exception):sqlstate='55P03'
 def busy(db):raise OperationalError('synthetic statement',{},Busy('Synthetic upload busy'))
 monkeypatch.setattr(upload_storage,'_lock_upload_workflow',busy)
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()
 response=upload(client,issue(['write:models']),'Synthetic busy.gcode',GCODE)
 assert response.status_code==409,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()==before
 assert not [p for p in storage.rglob('*') if p.is_file()]


def test_commit_and_rollback_failure_preserves_payload_and_safe_reference(upload_client,review_db,monkeypatch):
 from sqlalchemy.orm import Session
 client,issue,storage=upload_client;headers=issue(['write:models']);commit=Session.commit;rollback=Session.rollback;armed=[]
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()
 def refused_commit(self):
  if self.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()>before:
   armed.append(True);raise RuntimeError('Synthetic private commit details')
  return commit(self)
 def refused_rollback(self):
  if armed:raise RuntimeError('Synthetic private rollback details')
  return rollback(self)
 monkeypatch.setattr(Session,'commit',refused_commit);monkeypatch.setattr(Session,'rollback',refused_rollback)
 response=upload(client,headers,'Synthetic ambiguous rollback.gcode',GCODE)
 assert response.status_code==500
 assert 'outcome is uncertain' in response.json()['detail'] and 'file reference' in response.json()['detail']
 assert 'private' not in response.text
 files=[p for p in storage.rglob('*') if p.is_file()]
 assert len(files)==1 and files[0].read_bytes()==GCODE


def test_configured_root_uploaded_file_preview_returns_exact_bytes(upload_client,review_db):
 from modules.archives.routes import archives_crud
 client,issue,storage=upload_client;client.app.include_router(archives_crud.router)
 content=Path('tests/fixtures/release_gate/candidate-calibration-cube.3mf').read_bytes()
 response=upload(client,issue(['write:models']),'Synthetic preview.3mf',content)
 assert response.status_code==200,response.text
 file_id=response.json()['id']
 preview=client.get(f'/files/{file_id}/preview-model',headers=issue(['read:models']))
 assert preview.status_code==200,preview.text
 assert preview.content==content
 assert 'Synthetic%20preview.3mf' in preview.headers['content-disposition']


@pytest.mark.parametrize('kind',['outside','symlink_escape','sibling_prefix'])
def test_configured_upload_root_preview_does_not_expand_to_arbitrary_files(upload_client,review_db,tmp_path,kind):
 from modules.archives.routes import archives_crud
 client,issue,storage=upload_client;client.app.include_router(archives_crud.router)
 outside=tmp_path/'synthetic-private.3mf';outside.write_bytes(b'Synthetic must not be disclosed')
 if kind=='outside':path=outside
 elif kind=='sibling_prefix':
  sibling=storage.with_name(storage.name+'-sibling');sibling.mkdir();path=sibling/'synthetic.3mf';path.write_bytes(outside.read_bytes())
 else:
  path=storage/'synthetic-link.3mf';path.symlink_to(outside)
 review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(201,'Synthetic alias.3mf','Synthetic alias.3mf',:path,1)"),{'path':str(path)});review_db.commit()
 response=client.get('/files/201/preview-model',headers=issue(['read:models']))
 assert response.status_code==400,response.text
 assert b'Synthetic must not be disclosed' not in response.content


def test_relative_upload_root_is_rejected_without_graph_or_payload(upload_client,review_db,monkeypatch):
 client,issue,storage=upload_client;monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT','synthetic-relative-root')
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()
 response=upload(client,issue(['write:models']),'Synthetic relative.gcode',GCODE)
 assert response.status_code==400,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files')).scalar_one()==before
 assert not [p for p in storage.rglob('*') if p.is_file()]
 assert not Path('synthetic-relative-root').exists()
