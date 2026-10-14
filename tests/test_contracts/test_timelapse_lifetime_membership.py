"""Prepare-only original media preservation and storage enforcement."""
import importlib,os
from datetime import datetime,timezone
import pytest
from sqlalchemy import text,select,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_encoder_publication import encoder,encode,frame_writer,review_db,owned_runtime
from core.timelapse_retirement_membership_schema import bindings,members,receipts
from modules.archives.timelapse_lifetime_prepare import prepare_lifetime_retirement
from modules.archives.timelapse_retirement import RetirementUnavailable
from tests.test_contracts.test_timelapse_retirement_prepare import owned_plan
M=importlib.import_module('core.schema.migrations.023_timelapse_retirement_membership')
@pytest.fixture
def prepared_source(encoder,review_db):
 assert encode(encoder)
 writer,tid,root,oldnonce,_,_=encoder
 with review_db.get_bind().begin() as c:M.apply(c)
 nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one();review_db.rollback()
 return review_db.get_bind(),root,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'},nonce

def prepare(value):return prepare_lifetime_retirement(*value[:4],expected_nonce=value[4])
def files(root):return {str(p.relative_to(root)):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}

def test_prepare_real_receipts_and_gap_membership_preserves_files(prepared_source):
 before=files(prepared_source[1]);result=prepare(prepared_source)
 assert result.created and files(prepared_source[1])==before
 with Session(prepared_source[0]) as db:
  row=db.execute(select(bindings)).mappings().one();assert row['phase']=='sealed' and row['object_count']>0
  roles=db.execute(select(members.c.role)).scalars().all();assert roles.count('completed_frame')==3 and roles.count('current_video')==1
  assert db.execute(select(receipts)).first() is None
  assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='deleting'
  M.validate(db.connection())
 assert not prepare(prepared_source).created

@pytest.mark.parametrize('fault',['intent','binding','member','seal','audit','commit','ack'])
def test_prepare_rollback_and_ack_are_exact(prepared_source,monkeypatch,fault):
 from modules.archives import timelapse_lifetime_prepare as module
 before=files(prepared_source[1]);engine=prepared_source[0]
 targets={'intent':'timelapse_retirements','binding':bindings.name,'member':members.name,'audit':'audit_logs'}
 def fail(c,cur,sql,params,ctx,many):
  if fault in targets and sql.startswith('INSERT INTO '+targets[fault]):raise RuntimeError('owned failure')
  if fault=='seal' and sql.startswith('UPDATE '+bindings.name):raise RuntimeError('owned failure')
 class Fault(Session):
  def commit(self):
   if fault=='commit':raise RuntimeError('owned failure')
   super().commit()
   if fault=='ack':raise RuntimeError('owned ACK failure')
 monkeypatch.setattr(module,'Session',Fault);event.listen(engine,'before_cursor_execute',fail)
 try:
  with pytest.raises(RetirementUnavailable):prepare(prepared_source)
 finally:event.remove(engine,'before_cursor_execute',fail)
 assert files(prepared_source[1])==before
 with Session(engine) as db:assert db.execute(select(bindings)).first() is None if fault!='ack' else db.execute(select(bindings)).first() is not None
 if fault=='ack':
  monkeypatch.setattr(module,'Session',Session);assert not prepare(prepared_source).created

@pytest.mark.parametrize('mutation',['nonce','source','extra_frame','private_extra','namespace'])
def test_uncertainty_refuses_preserving_evidence(prepared_source,mutation):
 engine,root,tid,principal,nonce=prepared_source
 if mutation=='nonce':
  with pytest.raises(RetirementUnavailable):prepare_lifetime_retirement(engine,root,tid,principal,expected_nonce='stale')
  return
 if mutation=='source':next(root.glob('encode_*.mp4')).write_bytes(b'changed')
 elif mutation=='extra_frame':(next(root.glob('frames_*'))/'extra.jpg').write_bytes(b'unknown')
 elif mutation=='private_extra':(next(root.glob('publish_*'))/'extra').write_bytes(b'unknown')
 elif mutation=='namespace':
  space=next(root.glob('frames_*'));space.rename(root/'held');space.mkdir(mode=0o700)
 before=files(root)
 with pytest.raises(RetirementUnavailable):prepare(prepared_source)
 assert files(root)==before
 with Session(engine) as db:assert db.execute(select(bindings)).first() is None

@pytest.mark.parametrize('mutation',['early_complete','forged_receipt','member_update','member_delete','postseal_insert','binding_update','binding_delete'])
def test_sealed_storage_refuses_forged_progress(prepared_source,mutation):
 prepare(prepared_source);engine=prepared_source[0]
 with engine.connect() as c:
  if mutation=='early_complete':sql="UPDATE timelapse_retirements SET state='complete',completed_at=CURRENT_TIMESTAMP"
  elif mutation=='forged_receipt':
   m=c.execute(select(members).where(members.c.role=='frame_namespace')).mappings().one()
   with pytest.raises(Exception):c.execute(receipts.insert(),dict(operation_id=m['operation_id'],object_index=m['object_index'],role=m['role'],phase='complete',completed_at=datetime.now(timezone.utc)))
   c.rollback();return
  elif mutation=='member_update':sql="UPDATE timelapse_retirement_members SET fact_path='changed'"
  elif mutation=='member_delete':sql='DELETE FROM timelapse_retirement_members'
  elif mutation=='postseal_insert':
   m=dict(c.execute(select(members)).mappings().first())
   with pytest.raises(Exception):c.execute(members.insert(),m)
   c.rollback();return
  elif mutation=='binding_update':sql="UPDATE timelapse_retirement_lifetime_bindings SET lifetime_nonce='other'"
  else:sql='DELETE FROM timelapse_retirement_lifetime_bindings'
  with pytest.raises(Exception):c.exec_driver_sql(sql)
  c.rollback()
 with engine.begin() as c:M.validate(c)

@pytest.mark.parametrize('drift',['trigger','default','partial'])
def test_adoption_refuses_drift_without_repair(prepared_source,drift):
 with prepared_source[0].begin() as c:
  if drift=='trigger':c.exec_driver_sql('DROP TRIGGER timelapse_retirement_members_membership_update')
  elif drift=='partial':c.exec_driver_sql('DROP TABLE timelapse_retirement_container_receipts')
  else:c.exec_driver_sql('ALTER TABLE timelapse_retirement_container_receipts ADD COLUMN unexpected TEXT DEFAULT \'x\'')
  with pytest.raises(Exception):M.apply(c)

@pytest.mark.parametrize('orphan',[None,'frames_random_rollback','legacy'])
def test_failed_zero_has_no_fabricated_video_or_container_receipt(frame_writer,review_db,orphan):
 writer,tid,root,nonce=frame_writer
 review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
 assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
 with review_db.get_bind().begin() as c:M.apply(c)
 if orphan:
  path=root/(str(tid) if orphan=='legacy' else orphan);path.mkdir(mode=0o700);(path/'original.jpg').write_bytes(b'ORIGINAL')
 before=files(root);value=(review_db.get_bind(),root,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'},nonce)
 if orphan:
  with pytest.raises(RetirementUnavailable):prepare(value)
 else:
  assert prepare(value).created
  with Session(value[0]) as db:
   b=db.execute(select(bindings)).mappings().one();assert b['object_count']==0 and b['phase']=='sealed'
   assert db.execute(select(members)).first() is None and db.execute(select(receipts)).first() is None
   M.validate(db.connection())
 assert files(root)==before

def test_numeric_id_reuse_cannot_replay_original_version(prepared_source):
 result=prepare(prepared_source);engine,root,tid,principal,nonce=prepared_source
 before=files(root)
 with engine.begin() as c:
  c.execute(text('DELETE FROM timelapses WHERE id=:id'),{'id':tid})
  c.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,ownership_operation_id) VALUES(:id,77,'replacement.mp4','ready',NULL)"),{'id':tid})
 with pytest.raises(RetirementUnavailable):prepare(prepared_source)
 assert files(root)==before
 with Session(engine) as db:assert db.execute(select(bindings.c.version_nonce)).scalar_one()==nonce


def test_legacy_plan_dispatch_is_not_reinterpreted(review_db,owned_plan):
 from modules.archives.timelapse_retirement import prepare_retirement
 value=owned_plan
 result=prepare_retirement(*value[:4])
 with value[0].begin() as c:M.apply(c)
 before=files(value[1])
 with pytest.raises(RetirementUnavailable):prepare_lifetime_retirement(*value[:4],expected_nonce=result.operation_id)
 assert not prepare_retirement(*value[:4]).created
 assert files(value[1])==before

def test_zero_history_remains_valid_after_metadata_id_reuse(frame_writer,review_db):
 writer,tid,root,nonce=frame_writer
 review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
 assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
 engine=review_db.get_bind()
 with engine.begin() as c:M.apply(c)
 value=(engine,root,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'},nonce);prepare(value)
 with engine.begin() as c:
  c.execute(text('DELETE FROM timelapses WHERE id=:id'),{'id':tid})
  c.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(:id,77,'replacement.mp4','ready')"),{'id':tid})
  M.validate(c)
 with pytest.raises(RetirementUnavailable):prepare(value)

@pytest.fixture
def zero_source(frame_writer,review_db):
 writer,tid,root,nonce=frame_writer
 review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
 assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
 return (review_db.get_bind(),root,tid,{'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'},nonce)


def test_pruned_zero_audit_does_not_revoke_sealed_history_or_replay(zero_source):
 prepare(zero_source)
 with zero_source[0].begin() as c:
  c.execute(text("DELETE FROM audit_logs WHERE action='timelapse_encode_failed'"))
  M.validate(c)
 assert not prepare(zero_source).created
 with zero_source[0].begin() as c:
  c.execute(text('DELETE FROM timelapses WHERE id=:id'),{'id':zero_source[2]})
  c.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(:id,77,'replacement.mp4','ready')"),{'id':zero_source[2]})
  M.validate(c)
 with pytest.raises(RetirementUnavailable):prepare(zero_source)


def test_missing_terminal_audit_still_refuses_fresh_zero_admission(zero_source):
 with zero_source[0].begin() as c:c.execute(text("DELETE FROM audit_logs WHERE action='timelapse_encode_failed'"))
 with pytest.raises(RetirementUnavailable):prepare(zero_source)
 with zero_source[0].begin() as c:assert c.execute(select(bindings)).first() is None

@pytest.fixture
def planned_absence(zero_source):
 from uuid import uuid4
 from core.timelapse_media_lock import acquire_media_guard
 from core.timelapse_manifest import _root
 from core.timelapse_publication_schema import lifetimes,versions,namespaces
 engine,root,tid,principal,nonce=zero_source
 namespace=uuid4().hex
 with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity):
  with engine.begin() as c:
   c.execute(lifetimes.insert(),dict(lifetime_nonce=nonce,original_timelapse_id=tid,original_printer_id=77,**{'root_'+k:getattr(identity,k) for k in ('device','inode','uid','gid','mode')}))
   c.execute(versions.insert(),dict(version_nonce=nonce,lifetime_nonce=nonce))
   c.execute(namespaces.insert(),dict(namespace_id=namespace,lifetime_nonce=nonce,relative_path='frames_'+namespace,phase='planned'))
   guard.assert_owned()
 assert not (root/('frames_'+namespace)).exists()
 prepare(zero_source)
 return zero_source,namespace


def test_sealed_original_absence_is_not_a_physical_container_receipt(planned_absence):
 value,namespace=planned_absence
 from core.timelapse_retirement_membership_validation import completion_proof
 with value[0].begin() as c:
  b=c.execute(select(bindings)).mappings().one();assert b['object_count']==1
  m=c.execute(select(members)).mappings().one();assert m['role']=='frame_namespace'
  o=c.execute(text('SELECT * FROM timelapse_retirement_objects')).mappings().one()
  assert o['state']=='originally_absent' and o['relative_path']=='frames_'+namespace
  assert c.execute(select(receipts)).first() is None
  assert c.execute(text('SELECT 1 FROM timelapse_retirements n WHERE '+completion_proof('n'))).scalar_one()==1
  M.validate(c)
 # No completion mutation: future drain must recheck FD-root absence under the guard.


@pytest.mark.parametrize('mutation',['facts','path','late_object','delete_object','intent'])
def test_sealed_absence_and_identity_are_immutable(planned_absence,mutation):
 value,_=planned_absence
 with value[0].connect() as c:
  if mutation=='facts':sql="UPDATE timelapse_retirement_objects SET state='planned',device=1,inode=2,uid=1,gid=1,mode=16832,link_count=1,byte_count=0,mtime_ns=0,ctime_ns=0,quarantine_name='forged'"
  elif mutation=='path':sql="UPDATE timelapse_retirement_objects SET relative_path='other'"
  elif mutation=='late_object':sql="INSERT INTO timelapse_retirement_objects(operation_id,object_index,relative_path,kind,state) SELECT operation_id,7,'late','directory','originally_absent' FROM timelapse_retirement_lifetime_bindings"
  elif mutation=='delete_object':sql='DELETE FROM timelapse_retirement_objects'
  else:sql="UPDATE timelapse_retirements SET filename='changed'"
  with pytest.raises(Exception):c.exec_driver_sql(sql)
  c.rollback()
 with value[0].begin() as c:M.validate(c)
 assert not prepare(value).created


def test_originally_absent_namespace_cannot_mint_physical_receipt(planned_absence):
 value,_=planned_absence
 with value[0].connect() as c:
  m=c.execute(select(members)).mappings().one()
  with pytest.raises(Exception):c.execute(receipts.insert(),dict(operation_id=m['operation_id'],object_index=m['object_index'],role=m['role'],phase='complete',completed_at=datetime.now(timezone.utc)))
  c.rollback()
