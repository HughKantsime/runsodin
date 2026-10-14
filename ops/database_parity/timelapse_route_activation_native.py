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
  from fastapi import FastAPI
  from fastapi.testclient import TestClient
  from core.db import get_db
  from modules.printers import camera_routes as routes
  routes.TIMELAPSE_ROOT=str(root)
  app=FastAPI();app.include_router(routes.router)
  def database():
   with Session(engine) as db:yield db
  app.dependency_overrides[get_db]=database
  @app.middleware('http')
  async def audit(req,call_next):
   req.scope['odin.private.audit_actor_scope']=RequestAuditScope(None)
   return await call_next(req)
  with TestClient(app) as client:
   headers={'Authorization':'bEaReR '+token};tid=ticket.timelapse_id
   result=client.get(f'/timelapses/{tid}/stream',headers=headers)
   assert result.status_code==200,result.text
   original=result.content
   ranged=client.get(f'/timelapses/{tid}/video',headers={**headers,'Range':'bytes=2-19'})
   assert ranged.status_code==206 and ranged.content==original[2:20]
   assert not client.head(f'/timelapses/{tid}/download',headers=headers).content
   assert client.get(f'/timelapses/{tid}/stream',params={'token':token}).status_code==200
   assert client.get(f'/timelapses/{tid}/download',params={'token':token}).status_code==401
   assert client.delete(f'/timelapses/{tid}',headers=headers).status_code==428
   # All preparation predicates, including mutable terminal audit evidence, are fenced.
   with Session(engine) as owner:
    fence_authorization(owner,preparation=True);fresh_action_principal(owner,evidence)
    for relation in sorted(set(RELATIONS)|{'audit_logs'}):
     with engine.connect() as contender:
      contender.execute(text("SET LOCAL lock_timeout='30ms'"))
      try:contender.execute(text('INSERT INTO public.'+relation+' SELECT * FROM public.'+relation+' WHERE FALSE'))
      except Exception as error:
       assert getattr(error.orig,'sqlstate',None)=='55P03';contender.rollback()
      else:raise AssertionError('Insertion fence did not block '+relation)
    owner.rollback()
   assert client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"stale"'}).status_code==412
   retired=client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"'+nonce+'"'})
   assert retired.status_code==204,retired.text
   assert client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"'+nonce+'"'}).status_code==204
   assert client.get(f'/timelapses/{tid}/stream',headers=headers).status_code!=200
   with Session(engine) as db:
    assert db.execute(text('SELECT state FROM timelapse_retirements WHERE operation_id=:op'),{'op':nonce}).scalar_one()=='complete'
   assert not list(root.glob('frames_*')) and not list(root.glob('encode_*.mp4'))
 engine.dispose()
 print('timelapse-route-activation-native-pass: actualLinuxJPEG1/3/10-MP4/HTTP-selected-bearer-query/FD-range-HEAD/exact-nonce-DELETE-replay/all18-preparation-insertion-fences')
if __name__=='__main__':main()
