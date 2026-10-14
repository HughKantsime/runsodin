from core.interfaces.timelapse_media import require_timelapse_media_provider
"""Affected019 finding only: source drift during DB-closed verification."""
import pytest,os
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_route_activation import activated,prepared_source,encoder,frame_writer,review_db,owned_runtime
from modules.archives.timelapse_retirement import RetirementUnavailable

@pytest.mark.parametrize('action',['read','prepare'])
@pytest.mark.parametrize('mutation',['fact_update','fact_delete','fact_insert','lifetime_update'])
def test_source_drift_during_hash_refuses(action,mutation,activated,monkeypatch):
 from modules.archives import timelapse_read_lease as reader,timelapse_lifetime_prepare as prepare
 from modules.printers.timelapse_request_authority import selected_evidence
 from starlette.requests import Request
 from core.audit_actor import RequestAuditScope
 from core.dependencies import validate_access_token
 from core.timelapse_request_budget import VerificationBudget
 from core.timelapse_publication_schema import facts,versions
 client,engine,root,tid,nonce,token=activated
 request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
 with Session(engine) as db:evidence=selected_evidence(request,db,validate_access_token(token,db),provider=require_timelapse_media_provider(),purpose='read' if action=='read' else 'retire')
 fired=[]
 def change():
  if fired:return
  fired.append(True)
  with engine.begin() as c:
   if mutation=='fact_update':c.execute(text("UPDATE timelapse_publication_facts SET digest_sha256=:sha WHERE role='output' AND operation_id IN (SELECT operation_id FROM timelapse_publications WHERE kind='encode')"),{'sha':'0'*64})
   elif mutation=='fact_delete':c.execute(text("DELETE FROM timelapse_publication_facts WHERE role='output' AND operation_id IN (SELECT operation_id FROM timelapse_publications WHERE kind='encode')"))
   elif mutation=='fact_insert':
    row=dict(c.execute(facts.select().where(facts.c.role=='output',facts.c.operation_id.in_(text("SELECT operation_id FROM timelapse_publications WHERE kind='encode'")))).mappings().first());row['relative_path']='extra-owned-output.mp4';c.execute(facts.insert(),row)
   elif mutation=='lifetime_update':c.execute(text('UPDATE timelapse_media_lifetimes SET root_inode=root_inode+1'))
 if action=='read':
  actual=os.pread
  def pread(*args):
   result=actual(*args);change();return result
  monkeypatch.setattr(reader.os,'pread',pread)
  with pytest.raises(Exception):reader.admitted_response(engine,root,tid,evidence)
 else:
  actual=prepare.capture_evidence
  def capture(*args,**kwargs):
   result=actual(*args,**kwargs);change();return result
  monkeypatch.setattr(prepare,'capture_evidence',capture)
  with pytest.raises(RetirementUnavailable):prepare.prepare_request_lifetime_retirement(engine,root,tid,evidence,expected_nonce=nonce,budget=VerificationBudget())
 assert fired
 with Session(engine) as db:
  assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='ready'
  assert db.execute(text('SELECT count(*) FROM timelapse_retirements')).scalar_one()==0
