from core.interfaces.timelapse_media import require_timelapse_media_provider
"""Actual local HTTP authentication and owned publication route activation."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text
from core.db import get_db
from core.audit_actor import RequestAuditScope
from core.auth import create_access_token
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,encoder,frame_writer,review_db,owned_runtime

@pytest.fixture
def activated(prepared_source,monkeypatch):
 from modules.printers import camera_routes as routes
 from modules.archives import register_providers
 from core.registry import registry
 monkeypatch.setattr(registry,'_providers',dict(registry._providers))
 register_providers(registry)
 engine,root,tid,_,nonce=prepared_source
 monkeypatch.setattr(routes,'TIMELAPSE_ROOT',str(root))
 app=FastAPI();app.include_router(routes.router)
 def database():
  with Session(engine) as db:yield db
 app.dependency_overrides[get_db]=database
 @app.middleware('http')
 async def audit(request,call_next):
  request.scope['odin.private.audit_actor_scope']=RequestAuditScope(None)
  return await call_next(request)
 with Session(engine) as db:username=db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
 token=create_access_token({'sub':username,'role':'admin'})
 with TestClient(app) as client:yield client,engine,root,tid,nonce,token

def test_actual_bearer_fd_ranges_head_and_delete_replay(activated):
 client,engine,root,tid,nonce,token=activated;headers={'Authorization':'bEaReR '+token}
 response=client.get(f'/timelapses/{tid}/stream',headers=headers)
 assert response.status_code==200,response.text
 original=response.content;assert original[:4]!=b''
 ranged=client.get(f'/timelapses/{tid}/stream',headers={**headers,'Range':'bytes=2-9'})
 assert ranged.status_code==206 and ranged.content==original[2:10]
 head=client.head(f'/timelapses/{tid}/download',headers=headers)
 assert head.status_code==200 and not head.content
 assert client.get(f'/timelapses/{tid}/download',params={'token':token}).status_code==401
 assert client.get(f'/timelapses/{tid}/video',params={'token':token}).status_code==200
 assert client.delete(f'/timelapses/{tid}',headers=headers).status_code==428
 assert client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"stale"'}).status_code==412
 deleted=client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"'+nonce+'"'})
 assert deleted.status_code==204,deleted.text
 assert client.delete(f'/timelapses/{tid}',headers={**headers,'If-Match':'"'+nonce+'"'}).status_code==204
 assert client.get(f'/timelapses/{tid}/stream',headers=headers).status_code!=200

def test_read_purpose_cannot_retire_and_credential_revocation(activated):
 from starlette.requests import Request
 from modules.printers.timelapse_request_authority import selected_evidence
 from modules.archives.timelapse_action_authority import fresh_action_principal
 from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
 from modules.archives.timelapse_retirement import RetirementUnavailable
 from core.timelapse_request_budget import VerificationBudget
 client,engine,root,tid,nonce,token=activated
 request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
 with Session(engine) as db:
  from core.dependencies import validate_access_token
  principal=validate_access_token(token,db);evidence=selected_evidence(request,db,principal,provider=require_timelapse_media_provider(),purpose='read')
 with pytest.raises(RetirementUnavailable):prepare_request_lifetime_retirement(engine,root,tid,evidence,expected_nonce=nonce,budget=VerificationBudget())
 with engine.begin() as c:c.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:exp)'),{'jti':evidence.jti,'exp':'2099-01-01'})
 with Session(engine) as db:
  with pytest.raises(RetirementUnavailable):fresh_action_principal(db,evidence)

def test_actual_auth_precedence_query_restrictions_and_scopes(activated,monkeypatch):
 import secrets
 from core.auth import hash_password
 client,engine,root,tid,nonce,token=activated;path=f'/timelapses/{tid}/stream'
 # Normal dependency cookie rejection falls through to the selected bearer.
 client.cookies.set('session','invalid-owned-cookie')
 assert client.get(path,headers={'Authorization':'Bearer '+token}).status_code==200
 client.cookies.clear()
 limited=create_access_token({'sub':'unimportant','role':'admin','ws':True})
 # Existing terminal bearer rejection still permits the route's query fallback.
 assert client.get(path,headers={'Authorization':'Bearer '+limited},params={'token':token}).status_code==200
 assert client.get(f'/timelapses/{tid}/download',headers={'Authorization':'Bearer '+limited},params={'token':token}).status_code==401
 key='odin_'+secrets.token_hex(24)
 with engine.begin() as c:
  c.execute(text("INSERT INTO api_tokens(name,user_id,token_prefix,token_hash,scopes) VALUES('owned-read',3,:prefix,:hash,:scopes)"),{'prefix':key[:10],'hash':hash_password(key),'scopes':'["read:printers"]'})
 assert client.get(path,headers={'X-API-Key':key}).status_code==200
 assert client.delete(f'/timelapses/{tid}',headers={'X-API-Key':key,'If-Match':'"'+nonce+'"'}).status_code==403
 with engine.begin() as c:c.execute(text("UPDATE api_tokens SET scopes='[\"read:models\"]' WHERE token_prefix=:prefix"),{'prefix':key[:10]})
 assert client.get(path,headers={'X-API-Key':key}).status_code!=200
 # Legacy key remains supported, and no key is included in a receipt.
 legacy=secrets.token_hex(32);monkeypatch.setenv('API_KEY',legacy)
 assert client.get(path,headers={'X-API-Key':legacy}).status_code==200

def test_shared_prepare_budget_and_blacklist_during_capture_refuse(activated,monkeypatch):
 from starlette.requests import Request
 from modules.printers.timelapse_request_authority import selected_evidence
 from modules.archives import timelapse_lifetime_prepare as preparation
 from modules.archives.timelapse_retirement import RetirementUnavailable
 from core.timelapse_request_budget import VerificationBudget
 from core.dependencies import validate_access_token
 client,engine,root,tid,nonce,token=activated
 request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
 with Session(engine) as db:evidence=selected_evidence(request,db,validate_access_token(token,db),provider=require_timelapse_media_provider(),purpose='retire')
 budget=VerificationBudget();budget.remaining=0
 with pytest.raises(RetirementUnavailable):preparation.prepare_request_lifetime_retirement(engine,root,tid,evidence,expected_nonce=nonce,budget=budget)
 actual=preparation.capture_evidence
 def capture(*args,**kwargs):
  result=actual(*args,**kwargs)
  with engine.begin() as c:c.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:exp)'),{'jti':evidence.jti,'exp':'2099-01-01'})
  return result
 monkeypatch.setattr(preparation,'capture_evidence',capture)
 with pytest.raises(RetirementUnavailable):preparation.prepare_request_lifetime_retirement(engine,root,tid,evidence,expected_nonce=nonce,budget=VerificationBudget())
 with Session(engine) as db:
  assert db.execute(text('SELECT count(*) FROM timelapse_retirements')).scalar_one()==0
  assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='ready'
