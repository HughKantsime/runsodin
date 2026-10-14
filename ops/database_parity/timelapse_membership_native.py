"""Owned native prepare-only membership proof. No physical retirement."""
import os,importlib,tempfile,json,secrets
from pathlib import Path
os.environ["JWT_SECRET_KEY"]=secrets.token_urlsafe(48)
_config=tempfile.TemporaryDirectory(prefix='owned-membership-config-')
_password=Path(_config.name)/'password';_password.write_text(secrets.token_urlsafe(48));_password.chmod(0o600)
os.environ['DATABASE_PASSWORD_FILE']=str(_password)
os.environ['DATABASE_URL']=os.environ['MEMBERSHIP_NATIVE_URL'].replace('postgresql+psycopg://','postgresql://')
os.environ['ODIN_DATA_DIR']=_config.name
from sqlalchemy import create_engine,text,select
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.timelapse_retirement_membership_schema import bindings,members,receipts
from modules.archives.timelapse_capture import get_or_create_timelapse
from modules.archives.timelapse_encode import encode_owned_timelapse
from modules.archives.timelapse_lifetime_prepare import prepare_lifetime_retirement
from modules.archives.timelapse_retirement import RetirementUnavailable
from modules.printers.models import Printer
from modules.jobs.models import Job
M=importlib.import_module('core.schema.migrations.023_timelapse_retirement_membership')
def main():
 engine=create_engine(os.environ['MEMBERSHIP_NATIVE_URL'],hide_parameters=True)
 bootstrap_database(engine)
 with engine.begin() as c:M.apply(c)
 with engine.begin() as c:M.apply(c)
 # Actual storage transaction rollback, not a connection-level simulated receipt.
 with engine.connect() as c:
  c.exec_driver_sql('DROP TRIGGER timelapse_retirement_members_membership_guard ON timelapse_retirement_members')
  try:M.validate(c)
  except Exception:c.rollback()
  else:raise AssertionError('Missing trigger accepted')
 with engine.begin() as c:M.validate(c)
 with Session(engine) as db:
  printer=Printer(name='Owned membership',org_id=None,shared=True,api_type='synthetic',is_active=True,timelapse_enabled=True)
  db.add(printer);db.flush()
  fid=db.execute(text("INSERT INTO print_files(filename,original_filename,stored_path,org_id) VALUES('owned.3mf','owned.3mf','/owned/fixture.3mf',NULL) RETURNING id")).scalar_one()
  job=Job(item_name='Owned membership',printer_id=printer.id,print_file_id=fid,status='printing',charged_to_org_id=None)
  db.add(job);db.flush();pid,jid=printer.id,job.id
  uid=db.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) VALUES('owned-membership','invalid-owned-login-fixture','admin',TRUE,NULL) RETURNING id")).scalar_one()
  db.commit();ticket=get_or_create_timelapse(db,pid,jid,return_ticket=True)
  assert ticket is not None
  db.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':jid});db.commit()
 principal={'id':uid,'role':'admin','group_id':None,'_auth_kind':'session_jwt'}
 with tempfile.TemporaryDirectory(prefix='owned-membership-') as folder:
  root=Path(folder);root.chmod(0o700)
  assert not encode_owned_timelapse(sessions=lambda:Session(engine),root=root,timelapse_id=ticket.timelapse_id,printer_id=pid,nonce=ticket.nonce,job_id=jid)
  with Session(engine) as db:assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id}).scalar_one()=='failed'
  result=prepare_lifetime_retirement(engine,root,ticket.timelapse_id,principal,expected_nonce=ticket.nonce);assert result.created
  assert not prepare_lifetime_retirement(engine,root,ticket.timelapse_id,principal,expected_nonce=ticket.nonce).created
  try:prepare_lifetime_retirement(engine,root,ticket.timelapse_id,principal,expected_nonce='stale')
  except RetirementUnavailable:pass
  else:raise AssertionError('Stale caller accepted')
  with engine.begin() as c:
   row=c.execute(select(bindings)).mappings().one();assert row['phase']=='sealed' and row['object_count']==0
   assert c.execute(select(members)).first() is None and c.execute(select(receipts)).first() is None
   M.validate(c)
  with engine.begin() as c:
   c.execute(text("DELETE FROM audit_logs WHERE action='timelapse_encode_failed'"))
   M.validate(c)
  assert not prepare_lifetime_retirement(engine,root,ticket.timelapse_id,principal,expected_nonce=ticket.nonce).created
  bootstrap_database(engine) # audit pruning cannot invalidate sealed zero history
  for statement in (
   "UPDATE timelapse_retirement_lifetime_bindings SET object_count=1",
   "DELETE FROM timelapse_retirement_lifetime_bindings",
   "ALTER TABLE timelapse_retirement_members ADD COLUMN unsupported TEXT DEFAULT 'x'",
   "ALTER TABLE timelapse_retirement_lifetime_bindings DROP CONSTRAINT tmb_phase",
   "ALTER TABLE timelapse_retirement_members DISABLE TRIGGER timelapse_retirement_members_membership_guard",
  ):
   with engine.connect() as c:
    try:
     c.exec_driver_sql(statement)
     M.validate(c)
    except Exception:c.rollback()
    else:raise AssertionError('Mutation or adoption drift accepted')
  with engine.begin() as c:M.validate(c)
  # One actual journalled JPEG produces a failed, nonzero lifetime with physical members.
  from contextlib import contextmanager
  from types import SimpleNamespace
  import cv2,numpy as np
  from modules.archives.timelapse_frame_capture import capture_owned_frame
  with Session(engine) as db:
   db.execute(text("UPDATE jobs SET status='printing' WHERE id=:id"),{'id':jid});db.commit()
   second=get_or_create_timelapse(db,pid,jid,return_ticket=True)
  ok,jpeg=cv2.imencode('.jpg',np.full((32,32,3),99,dtype=np.uint8));assert ok
  @contextmanager
  def camera(*args,**kwargs):yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[jpeg.tobytes()])
  assert capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=camera),base='synthetic',printer_id=pid,timelapse_id=second.timelapse_id,nonce=second.nonce,job_id=jid)
  originals={str(p):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}
  with engine.begin() as c:c.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':jid})
  assert not encode_owned_timelapse(sessions=lambda:Session(engine),root=root,timelapse_id=second.timelapse_id,printer_id=pid,nonce=second.nonce,job_id=jid)
  assert prepare_lifetime_retirement(engine,root,second.timelapse_id,principal,expected_nonce=second.nonce).created
  with engine.connect() as c:
   try:c.execute(text("UPDATE timelapse_retirements SET state='complete',completed_at=CURRENT_TIMESTAMP WHERE operation_id=:op"),{'op':second.nonce})
   except Exception as e:assert getattr(e.orig,'sqlstate',None)=='23514';c.rollback()
   else:raise AssertionError('Early physical completion accepted')
   m=c.execute(select(members).where(members.c.operation_id==second.nonce,members.c.role=='frame_namespace')).mappings().one()
   try:c.execute(text("INSERT INTO timelapse_retirement_container_receipts(operation_id,object_index,role,phase,completed_at) VALUES(:op,:idx,'frame_namespace','complete',CURRENT_TIMESTAMP)"),{'op':second.nonce,'idx':m['object_index']})
   except Exception as e:assert getattr(e.orig,'sqlstate',None)=='23514';c.rollback()
   else:raise AssertionError('Forged physical receipt accepted')
  assert {str(p):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}==originals
  with engine.begin() as c:M.validate(c)
  # Reuse the original zero row ID through the real capture creator; old nonce cannot target it.
  with Session(engine) as db:
   db.execute(text('DELETE FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id})
   db.execute(text("SELECT setval(pg_get_serial_sequence('timelapses','id'),:id,false)"),{'id':ticket.timelapse_id})
   db.execute(text("UPDATE jobs SET status='printing' WHERE id=:id"),{'id':jid});db.commit()
   reused=get_or_create_timelapse(db,pid,jid,return_ticket=True)
   assert reused.timelapse_id==ticket.timelapse_id and reused.nonce!=ticket.nonce
  try:prepare_lifetime_retirement(engine,root,ticket.timelapse_id,principal,expected_nonce=ticket.nonce)
  except RetirementUnavailable:pass
  else:raise AssertionError('Reused ID accepted old nonce')
  with engine.begin() as c:M.validate(c)
  # A durably planned but never-created namespace has terminal sealed absence, not a physical receipt.
  from core.timelapse_media_lock import acquire_media_guard
  from core.timelapse_manifest import _root
  from core.timelapse_publication_schema import lifetimes,versions,namespaces
  from core.timelapse_retirement_membership_validation import completion_proof
  with engine.begin() as c:c.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':jid})
  assert not encode_owned_timelapse(sessions=lambda:Session(engine),root=root,timelapse_id=reused.timelapse_id,printer_id=pid,nonce=reused.nonce,job_id=jid)
  namespace=secrets.token_hex(16)
  with acquire_media_guard(root,reused.timelapse_id) as guard,_root(root) as (_,identity):
   with engine.begin() as c:
    c.execute(lifetimes.insert(),dict(lifetime_nonce=reused.nonce,original_timelapse_id=reused.timelapse_id,original_printer_id=pid,**{'root_'+k:getattr(identity,k) for k in ('device','inode','uid','gid','mode')}))
    c.execute(versions.insert(),dict(version_nonce=reused.nonce,lifetime_nonce=reused.nonce))
    c.execute(namespaces.insert(),dict(namespace_id=namespace,lifetime_nonce=reused.nonce,relative_path='frames_'+namespace,phase='planned'))
    guard.assert_owned()
  assert prepare_lifetime_retirement(engine,root,reused.timelapse_id,principal,expected_nonce=reused.nonce).created
  with engine.connect() as c:
   assert c.execute(text('SELECT 1 FROM timelapse_retirements n WHERE operation_id=:op AND '+completion_proof('n')),{'op':reused.nonce}).scalar_one()==1
   m=c.execute(select(members).where(members.c.operation_id==reused.nonce)).mappings().one()
   assert m['role']=='frame_namespace' and c.execute(select(receipts).where(receipts.c.operation_id==reused.nonce)).first() is None
   for statement in ("UPDATE timelapse_retirement_objects SET relative_path='changed' WHERE operation_id=:op", "UPDATE timelapse_retirements SET filename='changed' WHERE operation_id=:op"):
    try:c.execute(text(statement),{'op':reused.nonce})
    except Exception as e:assert getattr(e.orig,'sqlstate',None)=='23514';c.rollback()
    else:raise AssertionError('Sealed absence identity mutation accepted')
   try:c.execute(text("INSERT INTO timelapse_retirement_container_receipts(operation_id,object_index,role,phase,completed_at) VALUES(:op,:idx,'frame_namespace','complete',CURRENT_TIMESTAMP)"),{'op':reused.nonce,'idx':m['object_index']})
   except Exception as e:assert getattr(e.orig,'sqlstate',None)=='23514';c.rollback()
   else:raise AssertionError('Originally absent namespace fabricated physical receipt')
  assert not (root/('frames_'+namespace)).exists()
  bootstrap_database(engine) # actual registered023 startup validates sealed histories
 engine.dispose()
 print('timelapse-membership-native-pass: fullbootstrap/fresh/adopt/transactionrollback/real-zero-prepare/exact-ACK-replay/stale-nonce/no-fabricated-members/actual-JPEG-preserved/no-early-completion/forged-receipt-denied/actual-ID-reuse-denied/auditprune-startup-replay/immutable-sealed-absence/no-absent-physical-receipt/drift')
if __name__=='__main__':main()
