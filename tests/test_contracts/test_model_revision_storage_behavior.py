"""Actual revision HTTP/DB and owned filesystem failure boundaries."""
import builtins,uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_model_library_lifecycle_behavior import library_client,token_client,review_db

@pytest.fixture
def revision_storage(library_client,tmp_path,monkeypatch):
 from modules.models_library.routes import models_crud
 client,issue=library_client;storage=tmp_path/'owned-revision-storage'
 storage.mkdir()
 monkeypatch.setenv('MODEL_REVISION_STORAGE_ROOT',str(storage))
 return TestClient(client.app,raise_server_exceptions=False),issue,storage


def test_missing_revision_file_does_not_create_false_revert(revision_storage,review_db):
 client,issue,storage=revision_storage
 review_db.execute(text('UPDATE model_revisions SET file_path=:path WHERE model_id=101 AND revision_number=1'),{'path':str(storage/'101'/f'synthetic-missing-{uuid.uuid4().hex}.3mf')});review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()
 response=client.post('/models/101/revisions/1/revert',headers=issue(['write:models']))
 assert response.status_code in (400,404),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()==before


def test_oversize_revision_does_not_leave_empty_final_file(revision_storage,review_db):
 client,issue,storage=revision_storage
 before=review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()
 response=client.post('/models/101/revisions',headers=issue(['write:models']),files={'file':('synthetic-large.3mf',b'x'*(100*1024*1024+1),'application/octet-stream')})
 assert response.status_code==413,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()==before
 assert not [path for path in storage.rglob('*') if path.is_file()]


def test_revision_insert_failure_removes_new_file(revision_storage,review_db,monkeypatch):
 from sqlalchemy.orm import Session
 client,issue,storage=revision_storage;execute=Session.execute
 def refused(self,statement,*args,**kwargs):
  if 'INSERT INTO model_revisions' in str(statement):raise RuntimeError('Synthetic revision insert refused')
  return execute(self,statement,*args,**kwargs)
 monkeypatch.setattr(Session,'execute',refused)
 before=review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()
 response=client.post('/models/101/revisions',headers=issue(['write:models']),files={'file':('synthetic-small.3mf',b'Synthetic file preservation boundary','application/octet-stream')})
 assert response.status_code==500
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()==before
 assert not [path for path in storage.rglob('*') if path.is_file()]


def upload_revision(client,issue,payload=b'Synthetic original bytes'):
 return client.post('/models/101/revisions',headers=issue(['write:models']),files={'file':('synthetic.3mf',payload,'application/octet-stream')})


def test_revision_upload_and_revert_preserve_exact_bytes_and_source(revision_storage,review_db):
 from pathlib import Path
 client,issue,storage=revision_storage;payload=b'Synthetic payload\x00\xff'
 first=upload_revision(client,issue,payload)
 assert first.status_code==200,first.text
 source=Path(first.json()['file_path']);assert source.parent==storage/'101' and source.read_bytes()==payload
 second=upload_revision(client,issue,b'Synthetic different later payload')
 assert second.status_code==200 and second.json()['revision_number']==3
 later=Path(second.json()['file_path']);assert later.read_bytes()==b'Synthetic different later payload'
 response=client.post('/models/101/revisions/2/revert',headers=issue(['write:models']))
 assert response.status_code==200 and response.json()=={'revision_number':4,'reverted_from':2}
 destination=Path(review_db.execute(text('SELECT file_path FROM model_revisions WHERE model_id=101 AND revision_number=4')).scalar_one())
 assert later.read_bytes()==b'Synthetic different later payload'
 assert destination!=source and destination.read_bytes()==source.read_bytes()==payload


@pytest.mark.parametrize('kind',['outside','other_model','symlink_escape','directory'])
def test_revision_revert_rejects_non_model_payload_without_changes(revision_storage,review_db,tmp_path,kind):
 client,issue,storage=revision_storage
 outside=tmp_path/'synthetic-source';outside.write_bytes(b'Synthetic preserved outside payload')
 directory=storage/'101';directory.mkdir()
 if kind=='outside':source=outside
 elif kind=='other_model':
  parent=storage/'103';parent.mkdir();source=parent/'synthetic.3mf';source.write_bytes(b'Synthetic other model')
 elif kind=='symlink_escape':
  source=directory/'synthetic-link.3mf';source.symlink_to(outside)
 else:source=directory/'synthetic-dir';source.mkdir()
 review_db.execute(text('UPDATE model_revisions SET file_path=:path WHERE model_id=101 AND revision_number=1'),{'path':str(source)});review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()
 response=client.post('/models/101/revisions/1/revert',headers=issue(['write:models']))
 assert response.status_code in (400,404),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()==before
 assert outside.read_bytes()==b'Synthetic preserved outside payload'
 assert not [p for p in directory.glob('v*') if p.is_file()]


def test_partial_revert_copy_failure_removes_only_new_destination(revision_storage,review_db,monkeypatch):
 import shutil
 from pathlib import Path
 client,issue,storage=revision_storage;first=upload_revision(client,issue)
 assert first.status_code==200,first.text
 source=Path(first.json()['file_path']);before={p for p in storage.rglob('*') if p.is_file()}
 def refused(original,destination,*args,**kwargs):
  destination.write(b'Synthetic partial copy');raise OSError('Synthetic private path must not escape')
 monkeypatch.setattr(shutil,'copyfileobj',refused)
 response=client.post('/models/101/revisions/2/revert',headers=issue(['write:models']))
 assert response.status_code==500 and 'private path' not in response.text
 assert {p for p in storage.rglob('*') if p.is_file()}==before
 assert source.read_bytes()==b'Synthetic original bytes'
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==2


def test_revision_audit_failure_rolls_back_row_and_new_file(revision_storage,review_db,monkeypatch):
 from modules.models_library.routes import models_crud
 client,issue,storage=revision_storage
 def refused(*args,**kwargs):raise RuntimeError('Synthetic audit refused')
 monkeypatch.setattr(models_crud,'log_audit',refused)
 response=upload_revision(client,issue)
 assert response.status_code==500
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==1
 assert not [p for p in storage.rglob('*') if p.is_file()]


@pytest.mark.parametrize('committed',[False,True])
def test_uncertain_revision_commit_preserves_payload_for_reconciliation(revision_storage,review_db,monkeypatch,committed):
 from sqlalchemy.orm import Session
 client,issue,storage=revision_storage;headers=issue(['write:models']);commit=Session.commit;armed=[]
 def uncertain(self):
  if self.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()>1 and not armed:
   armed.append(True)
   if committed:commit(self)
   raise RuntimeError('Synthetic commit acknowledgement unavailable')
  return commit(self)
 monkeypatch.setattr(Session,'commit',uncertain)
 response=client.post('/models/101/revisions',headers=headers,files={'file':('synthetic.3mf',b'Synthetic uncertain payload','application/octet-stream')})
 assert response.status_code==500
 files=[p for p in storage.rglob('*') if p.is_file()]
 assert len(files)==1 and files[0].read_bytes()==b'Synthetic uncertain payload'
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==1+int(committed)


def test_revision_lock_refresh_denies_changed_parent_before_file_creation(revision_storage,review_db,monkeypatch):
 from sqlalchemy.orm import Session
 client,issue,storage=revision_storage;execute=Session.execute;seen=[]
 def changed(self,statement,*args,**kwargs):
  if str(statement).startswith('UPDATE models SET id=id') and not seen:
   seen.append(True);execute(self,text('UPDATE models SET org_id=2 WHERE id=101'));self.commit()
  return execute(self,statement,*args,**kwargs)
 monkeypatch.setattr(Session,'execute',changed)
 response=upload_revision(client,issue)
 assert response.status_code==404,response.text
 assert seen==[True]
 assert not [p for p in storage.rglob('*') if p.is_file()]
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==1


def test_revision_rejects_sibling_model_directory_alias(revision_storage,review_db):
 client,issue,storage=revision_storage
 sibling=storage/'103';sibling.mkdir();original=sibling/'synthetic-original.3mf';original.write_bytes(b'Synthetic other model preserved')
 (storage/'101').symlink_to(sibling,target_is_directory=True)
 response=upload_revision(client,issue)
 assert response.status_code==400,response.text
 assert original.read_bytes()==b'Synthetic other model preserved'
 assert [p.name for p in sibling.iterdir()]==['synthetic-original.3mf']
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==1


def test_real_audit_flush_failure_removes_payload_and_revision_row(revision_storage,review_db,monkeypatch):
 from modules.models_library.routes import models_crud
 from core.models import AuditLog
 client,issue,storage=revision_storage
 def invalid_audit(db,*args,**kwargs):
  db.add(AuditLog(action=None,entity_type='model',entity_id=101))
 monkeypatch.setattr(models_crud,'log_audit',invalid_audit)
 response=upload_revision(client,issue)
 assert response.status_code==500,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==1
 assert not [p for p in storage.rglob('*') if p.is_file()]


def test_revision_commit_and_rollback_failure_preserves_payload_and_safe_reference(revision_storage,review_db,monkeypatch):
 from sqlalchemy.orm import Session
 client,issue,storage=revision_storage;headers=issue(['write:models']);commit=Session.commit;rollback=Session.rollback;armed=[]
 def refused_commit(self):
  if self.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()>1:
   armed.append(True);raise RuntimeError('Synthetic private commit details')
  return commit(self)
 def refused_rollback(self):
  if armed:raise RuntimeError('Synthetic private rollback details')
  return rollback(self)
 monkeypatch.setattr(Session,'commit',refused_commit);monkeypatch.setattr(Session,'rollback',refused_rollback)
 response=client.post('/models/101/revisions',headers=headers,files={'file':('synthetic.3mf',b'Synthetic ambiguous revision','application/octet-stream')})
 assert response.status_code==500
 assert 'outcome is uncertain' in response.json()['detail'] and 'model reference 101' in response.json()['detail']
 assert 'private' not in response.text
 files=[p for p in storage.rglob('*') if p.is_file()]
 assert len(files)==1 and files[0].read_bytes()==b'Synthetic ambiguous revision'


def test_relative_revision_root_is_rejected_without_revision_or_payload(revision_storage,review_db,monkeypatch):
 from pathlib import Path
 client,issue,storage=revision_storage;monkeypatch.setenv('MODEL_REVISION_STORAGE_ROOT','synthetic-relative-revision')
 response=upload_revision(client,issue)
 assert response.status_code==400,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions WHERE model_id=101')).scalar_one()==1
 assert not [p for p in storage.rglob('*') if p.is_file()]
 assert not Path('synthetic-relative-revision').exists()
