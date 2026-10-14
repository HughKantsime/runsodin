"""Owned registered native physical service and actual insertion-fence proof."""
import os,tempfile,secrets
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
_config=tempfile.TemporaryDirectory(prefix='owned-lifetime-action-config-')
_password=Path(_config.name)/'password';_password.write_text(secrets.token_urlsafe(48));_password.chmod(0o600)
os.environ['DATABASE_PASSWORD_FILE']=str(_password)
os.environ['DATABASE_URL']=os.environ['MEMBERSHIP_NATIVE_URL'].replace('postgresql+psycopg://','postgresql://')
os.environ['ODIN_DATA_DIR']=_config.name
from sqlalchemy import create_engine,text,select
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.audit_actor import RequestAuditScope
from core.auth import create_access_token
from starlette.requests import Request
from core.timelapse_publication_schema import lifetimes,namespaces,versions
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from modules.archives.timelapse_frame_capture import capture_owned_frame,_capture_authority
from modules.archives.timelapse_frame_publication import FrameJournal
from modules.archives.timelapse_capture import get_or_create_timelapse
from modules.archives.timelapse_encode import encode_owned_timelapse
from modules.archives.timelapse_lifetime_prepare import prepare_lifetime_retirement
from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime,sealed_lifetime_receipt
from modules.archives.timelapse_action_authority import capture_action_credential,fence_authorization,fresh_action_principal,RELATIONS
from modules.printers.models import Printer
from modules.jobs.models import Job
import cv2,numpy as np

def main():
 engine=create_engine(os.environ['MEMBERSHIP_NATIVE_URL'],hide_parameters=True)
 bootstrap_database(engine)
 with Session(engine) as db:
  printer=Printer(name='Owned physical retirement',org_id=None,shared=True,api_type='synthetic',is_active=True,timelapse_enabled=True)
  db.add(printer);db.flush()
  fid=db.execute(text("INSERT INTO print_files(filename,original_filename,stored_path,org_id) VALUES('owned.3mf','owned.3mf','/owned/action.3mf',NULL) RETURNING id")).scalar_one()
  job=Job(item_name='Owned action',printer_id=printer.id,print_file_id=fid,status='printing',charged_to_org_id=None)
  db.add(job);db.flush();pid,jid=printer.id,job.id
  uid=db.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) VALUES('owned-action','invalid-fixture','admin',TRUE,NULL) RETURNING id")).scalar_one()
  db.commit();ticket=get_or_create_timelapse(db,pid,jid,return_ticket=True)
 principal={'id':uid,'role':'admin','group_id':None,'_auth_kind':'bearer_jwt'}
 token=create_access_token({'sub':'owned-action','role':'admin'})
 request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
 with Session(engine) as db:evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer')
 with tempfile.TemporaryDirectory(prefix='owned-lifetime-action-') as folder:
  root=Path(folder);root.chmod(0o700)
  ok,jpeg=cv2.imencode('.jpg',np.full((32,32,3),99,dtype=np.uint8));assert ok
  @contextmanager
  def camera(*args,**kwargs):yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kw:[jpeg.tobytes()])
  for number in range(1,11):
   if number in (1,3,10):
    assert capture_owned_frame(sessions=lambda:Session(engine),root=root,http=SimpleNamespace(stream=camera),base='synthetic',printer_id=pid,timelapse_id=ticket.timelapse_id,nonce=ticket.nonce,job_id=jid)
   else:
    with acquire_media_guard(root,ticket.timelapse_id) as guard,_root(root) as (fd,identity):
     def authority(db,*,allow_ended):return _capture_authority(db,nonce=ticket.nonce,timelapse_id=ticket.timelapse_id,printer_id=pid,job_id=jid,allow_ended=allow_ended)
     FrameJournal(sessions=lambda:Session(engine),root_fd=fd,root_identity=identity,guard=guard,authority=authority,nonce=ticket.nonce,timelapse_id=ticket.timelapse_id,printer_id=pid).admit(b'owned unpublished gap')
  with engine.begin() as c:c.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':jid})
  assert encode_owned_timelapse(sessions=lambda:Session(engine),root=root,timelapse_id=ticket.timelapse_id,printer_id=pid,nonce=ticket.nonce,job_id=jid)
  with Session(engine) as db:nonce=db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id}).scalar_one()
  originals={str(p):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}
  prepare_lifetime_retirement(engine,root,ticket.timelapse_id,principal,expected_nonce=nonce)
  # Actual DML requests cannot insert even previously absent rows while fenced.
  with Session(engine) as owner:
   fence_authorization(owner);fresh_action_principal(owner,evidence)
   for relation in RELATIONS:
    with engine.connect() as contender:
     contender.execute(text("SET LOCAL lock_timeout='30ms'"))
     try:contender.execute(text('INSERT INTO public.'+relation+' SELECT * FROM public.'+relation+' WHERE FALSE'))
     except Exception as error:
      assert getattr(error.orig,'sqlstate',None)=='55P03';contender.rollback()
     else:raise AssertionError('Insertion fence did not block '+relation)
   owner.rollback()
  # An outside-lifetime namespace reference commits after fence release, then rejects.
  with acquire_media_guard(root,ticket.timelapse_id),_root(root) as (_,identity):
   with Session(engine) as db:
    db.execute(text("UPDATE jobs SET status='printing' WHERE id=:id"),{'id':jid});db.commit()
    outside=get_or_create_timelapse(db,pid,jid,return_ticket=True)
   other=outside.nonce;space=secrets.token_hex(16)
   owned=next(root.glob('frames_*')).name
   with engine.begin() as c:
    c.execute(lifetimes.insert(),dict(lifetime_nonce=other,original_timelapse_id=outside.timelapse_id,original_printer_id=pid,**{'root_'+k:getattr(identity,k) for k in ('device','inode','uid','gid','mode')}))
    c.execute(versions.insert(),dict(version_nonce=other,lifetime_nonce=other))
    c.execute(namespaces.insert(),dict(namespace_id=space,lifetime_nonce=other,relative_path=owned+'/outside',phase='planned'))
  try:retire_sealed_lifetime(engine,root,ticket.timelapse_id,evidence,expected_nonce=nonce)
  except Exception as error:assert getattr(error,'reason',None)=='shared_workspace'
  else:raise AssertionError('Outside reference accepted')
  assert originals=={str(p):(p.stat().st_ino,p.read_bytes()) for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}
  # The independently created planned fixture is caller-owned DB evidence only.
  # Keep its row and move only this fixture-created planned reference back to its own path.
  with engine.begin() as c:
   c.execute(text("UPDATE timelapse_frame_namespaces SET relative_path=:path WHERE namespace_id=:id"),{'path':'frames_'+space,'id':space})
  from modules.archives import timelapse_lifetime_action as service
  from core import timelapse_action_fs as action_fs
  verify=service.verify_action;rename=action_fs.rename_noreplace;fired=[];syscalls=[]
  def replaced_parent(*args,**kwargs):
   result=verify(*args,**kwargs)
   if not fired:
    fired.append((result.row['object_index'],result.snapshot['intent']['revision']))
    (root/owned).rename(root/'preserved-frame-parent');(root/owned).mkdir(mode=0o700)
   return result
  def tracked_rename(*args):syscalls.append(True);return rename(*args)
  service.verify_action=replaced_parent;action_fs.rename_noreplace=tracked_rename
  try:
   try:retire_sealed_lifetime(engine,root,ticket.timelapse_id,evidence,expected_nonce=nonce)
   except Exception:pass
   else:raise AssertionError('Replaced pinned frame parent accepted')
   assert fired and not syscalls and len(list((root/'preserved-frame-parent').glob('*.jpg')))==3
   with engine.begin() as c:
    assert c.execute(text('SELECT revision FROM timelapse_retirements WHERE operation_id=:op'),{'op':nonce}).scalar_one()==fired[0][1]
  finally:service.verify_action=verify;action_fs.rename_noreplace=rename
  (root/owned).rmdir();(root/'preserved-frame-parent').rename(root/owned)
  assert retire_sealed_lifetime(engine,root,ticket.timelapse_id,evidence,expected_nonce=nonce).state=='complete'
  assert sealed_lifetime_receipt(engine,evidence,operation_id=nonce).state=='complete'
  assert not list(root.glob('frames_*')) and not list(root.glob('encode_*.mp4'))
  with Session(engine) as db:
   zero_job=Job(item_name='Owned zero action',printer_id=pid,print_file_id=fid,status='printing',charged_to_org_id=None)
   db.add(zero_job);db.flush();zero_jid=zero_job.id;db.commit()
   zero=get_or_create_timelapse(db,pid,zero_jid,return_ticket=True)
   db.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':zero_jid});db.commit()
  assert not encode_owned_timelapse(sessions=lambda:Session(engine),root=root,timelapse_id=zero.timelapse_id,printer_id=pid,nonce=zero.nonce,job_id=zero_jid)
  prepare_lifetime_retirement(engine,root,zero.timelapse_id,principal,expected_nonce=zero.nonce)
  assert retire_sealed_lifetime(engine,root,zero.timelapse_id,evidence,expected_nonce=zero.nonce).state=='complete'
  with engine.begin() as c:
   assert c.execute(text('SELECT count(*) FROM timelapse_retirement_container_receipts WHERE operation_id=:op'),{'op':zero.nonce}).scalar_one()==0
  bootstrap_database(engine)
 engine.dispose()
 print('timelapse-lifetime-action-native-pass: realLinuxJPEG1/3/10+MP4/sealed-stage-drain/pinned-parent-replacement-no-syscall/atomic-completion/zero-inflight-completion/all17-insertion-fences/outside-reference-reauth-refusal/completed-replay')
if __name__=='__main__':main()
