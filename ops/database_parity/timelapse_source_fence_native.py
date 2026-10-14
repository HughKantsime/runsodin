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
  from modules.archives import timelapse_read_lease as reader,timelapse_lifetime_prepare as preparer
  from core.timelapse_request_budget import VerificationBudget
  from modules.archives.timelapse_retirement import RetirementUnavailable
  from core.timelapse_publication_schema import facts as source_facts,lifetimes as source_lives,publications as source_publications
  with Session(engine) as db:
   read_evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer',purpose='read')
   output=dict(db.execute(select(source_facts).where(source_facts.c.role=='output',source_facts.c.operation_id.in_(select(source_publications.c.operation_id).where(source_publications.c.kind=='encode')))).mappings().one())
   life=dict(db.execute(select(source_lives)).mappings().one())
  # Inject actual DML at final source-row locking, while every table fence is held.
  def contention(original):
   def locked(db,groups):
    original(db,groups)
    statements=[
     'UPDATE timelapse_media_lifetimes SET root_inode=root_inode',
     'DELETE FROM timelapse_media_lifetimes',
     'UPDATE timelapse_media_versions SET lifetime_nonce=lifetime_nonce',
     'DELETE FROM timelapse_media_versions',
     'UPDATE timelapse_publication_facts SET digest_sha256=digest_sha256',
     'DELETE FROM timelapse_publication_facts',
     "INSERT INTO timelapse_publication_facts SELECT operation_id,role,relative_path||'_owned_child',kind,device,inode,uid,gid,mode,link_count,byte_count,mtime_ns,ctime_ns,digest_sha256 FROM timelapse_publication_facts WHERE role='output' LIMIT 1"]
    for sql in statements:
     with engine.connect() as contender:
      contender.execute(text("SET LOCAL lock_timeout='30ms'"))
      try:contender.execute(text(sql))
      except Exception as error:
       assert getattr(error.orig,'sqlstate',None)=='55P03';contender.rollback()
      else:raise AssertionError('Source DML was not fenced')
    for table,rows in groups:
     if not rows:continue
     row=rows[0]
     with engine.connect() as contender:
      try:contender.execute(select(table).where(*(key==row[key.name] for key in table.primary_key.columns)).with_for_update(nowait=True))
      except Exception as error:
       assert getattr(error.orig,'sqlstate',None)=='55P03';contender.rollback()
      else:raise AssertionError('Selected source row not locked')
   return locked
  original_lock=reader.lock_media_sources;reader.lock_media_sources=contention(original_lock)
  response=reader.admitted_response(engine,root,ticket.timelapse_id,read_evidence);response.close();reader.lock_media_sources=original_lock
  # Real committed hash->final source drift is refused for reads and preparation.
  original_pread=os.pread;original_capture=preparer.capture_evidence
  for action in ('read','prepare'):
   for mutation in ('update','delete','insert','lifetime'):
    fired=[];committed=[]
    def change():
     if fired:return
     fired.append(True)
     with engine.begin() as c:
      condition=(source_facts.c.operation_id==output['operation_id'])&(source_facts.c.role==output['role'])&(source_facts.c.relative_path==output['relative_path'])
      if mutation=='update':c.execute(source_facts.update().where(condition).values(digest_sha256='0'*64))
      elif mutation=='delete':c.execute(source_facts.delete().where(condition))
      elif mutation=='insert':c.execute(source_facts.insert(),dict(output,relative_path='extra_owned_output.mp4'))
      else:c.execute(source_lives.update().where(source_lives.c.lifetime_nonce==life['lifetime_nonce']).values(root_inode=life['root_inode']+1))
     committed.append(True)
    def pread(*args):
     value=original_pread(*args);change();return value
    def capture(*args,**kwargs):
     value=original_capture(*args,**kwargs);change();return value
    if action=='read':os.pread=pread
    else:preparer.capture_evidence=capture
    refused=False
    try:
     if action=='read':
      result=reader.admitted_response(engine,root,ticket.timelapse_id,read_evidence);result.close()
     else:preparer.prepare_request_lifetime_retirement(engine,root,ticket.timelapse_id,evidence,expected_nonce=nonce,budget=VerificationBudget())
    except Exception:refused=True
    finally:os.pread=original_pread;preparer.capture_evidence=original_capture
    assert refused and fired and committed
    with engine.begin() as c:
     assert c.execute(text('SELECT count(*) FROM timelapse_retirements')).scalar_one()==0
     if mutation=='delete':c.execute(source_facts.insert(),output)
     elif mutation=='insert':c.execute(source_facts.delete().where(source_facts.c.relative_path=='extra_owned_output.mp4'))
     elif mutation=='update':c.execute(source_facts.update().where(source_facts.c.operation_id==output['operation_id'],source_facts.c.role==output['role'],source_facts.c.relative_path==output['relative_path']).values(digest_sha256=output['digest_sha256']))
     else:c.execute(source_lives.update().where(source_lives.c.lifetime_nonce==life['lifetime_nonce']).values(root_inode=life['root_inode']))
  original_lock=preparer.lock_media_sources;preparer.lock_media_sources=contention(original_lock)
  result=preparer.prepare_request_lifetime_retirement(engine,root,ticket.timelapse_id,evidence,expected_nonce=nonce,budget=VerificationBudget())
  assert result.created;preparer.lock_media_sources=original_lock
  with engine.begin() as c:assert c.execute(text("SELECT phase FROM timelapse_retirement_lifetime_bindings WHERE operation_id=:op"),{'op':nonce}).scalar_one()=='sealed'
 engine.dispose()
 print('timelapse-source-fence-native-pass: current025-bootstrap/read-and-prepare-actual-source-UPDATE-DELETE-newchildINSERT-55P03/selected-source-row-NOWAIT/8-committed-hash-final-drift-refusals')
if __name__=='__main__':main()
