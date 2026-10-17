"""Actual variant metadata respects both model and file ownership/audience."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def variants_client(token_client,review_db,monkeypatch):
 from modules.models_library.routes import models_crud
 from core.registry import registry
 from modules.organizations.services import EducationPolicyService
 client,issue=token_client;client.app.include_router(models_crud.router)
 monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
 review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(101,'Synthetic own variants',1),(103,'Synthetic global variants',NULL)"))
 for identifier,model,org in [(201,101,1),(202,101,2),(203,101,None),(204,103,2),(205,103,1)]:
  review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,model_id,org_id,stored_path,plate_count) VALUES(:id,:name,:name,:model,:org,:path,2)"),{'id':identifier,'model':model,'org':org,'name':f'Synthetic variant {identifier}','path':f'/opaque/variant-{identifier}.3mf'})
 review_db.execute(text('UPDATE print_files SET model_id=12 WHERE id=11'))
 review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"));review_db.commit()
 return client,issue

@pytest.mark.parametrize('model,expected',[(101,{201,203}),(103,{205})])
def test_variant_list_hides_foreign_child_even_when_parent_is_accessible(variants_client,model,expected):
 client,issue=variants_client
 response=client.get(f'/models/{model}/variants',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 assert {row['id'] for row in response.json()['variants']}==expected
 assert all(row['plate_count']==2 for row in response.json()['variants'])

@pytest.mark.parametrize('user_id,expected',[(1,{11}),(4,set()),(3,{11})])
def test_variant_education_audience_matches_owner_or_manager(variants_client,review_db,user_id,expected):
 client,issue=variants_client;headers=issue(['read:models'])
 review_db.execute(text('UPDATE api_tokens SET user_id=:uid WHERE id=(SELECT MAX(id) FROM api_tokens)'),{'uid':user_id});review_db.commit()
 response=client.get('/models/12/variants',headers=headers)
 assert response.status_code==200,response.text
 assert {row['id'] for row in response.json()['variants']}==expected


def test_variant_education_authority_absence_fails_closed(variants_client,monkeypatch):
 from core.registry import registry
 client,issue=variants_client;monkeypatch.setitem(registry._providers,'EducationPolicyProvider',None)
 response=client.get('/models/12/variants',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 assert response.json()['variants']==[]


def test_variant_education_file_cannot_be_deleted_through_generic_endpoint(variants_client,review_db):
 client,issue=variants_client
 before=review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=11')).scalar_one()
 response=client.delete('/models/12/variants/11',headers=issue(['write:models']))
 assert response.status_code in (404,409),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=11')).scalar_one()==before

@pytest.mark.parametrize('state',['reserved','streaming','staged','renamed','committed','aborted','needs_reconciliation','purge_pending','purged'])
@pytest.mark.parametrize('user_id',[2,3])
def test_operation_path_alias_protects_generic_variant_delete_in_every_state(variants_client,review_db,state,user_id):
 client,issue=variants_client
 if user_id==2:
  review_db.execute(text("UPDATE users SET role='operator' WHERE id=2"));review_db.commit()
 review_db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('synthetic-variant-alias',1,1,:state,'/opaque/variant-201.3mf')"),{'state':state})
 headers=issue(['write:models'])
 review_db.execute(text('UPDATE api_tokens SET user_id=:uid WHERE id=(SELECT MAX(id) FROM api_tokens)'),{'uid':user_id});review_db.commit()
 response=client.delete('/models/101/variants/201',headers=headers)
 # models.delete is admin-only by default: the operator is refused before the alias check; the admin gets 404
 assert response.status_code==(403 if user_id==2 else 404),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=201')).scalar_one()==1
 assert review_db.execute(text("SELECT final_path FROM education_upload_operations WHERE operation_id='synthetic-variant-alias'")).scalar_one()=='/opaque/variant-201.3mf'


def test_revoked_manager_cannot_read_private_variant_metadata(variants_client,review_db):
 client,issue=variants_client;headers=issue(['read:models'])
 review_db.execute(text('UPDATE api_tokens SET user_id=2 WHERE id=(SELECT MAX(id) FROM api_tokens)'))
 review_db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2"));review_db.commit()
 response=client.get('/models/12/variants',headers=headers)
 assert response.status_code==200 and response.json()['variants']==[]


def test_owned_generic_variant_delete_preserves_foreign_child_and_reports_remaining(variants_client,review_db):
 client,issue=variants_client
 response=client.delete('/models/101/variants/201',headers=issue(['write:models']))
 assert response.status_code==200,response.text
 assert response.json()['remaining']==1
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=201')).scalar_one()==0
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=202')).scalar_one()==1


def test_variant_metadata_comes_from_authorized_snapshot(variants_client,review_db,monkeypatch):
 from modules.models_library.routes import models_crud
 client,issue=variants_client;authorize=models_crud.accessible_preview_file
 def changed(db,file_id,principal):
  row=authorize(db,file_id,principal)
  if file_id==201:
   db.execute(text("UPDATE print_files SET filename='Synthetic foreign replacement',org_id=2 WHERE id=201"))
  return row
 monkeypatch.setattr(models_crud,'accessible_preview_file',changed)
 response=client.get('/models/101/variants',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 own=next(row for row in response.json()['variants'] if row['id']==201)
 assert own['filename']=='Synthetic variant 201'
 assert 'Synthetic foreign replacement' not in response.text

@pytest.mark.parametrize('change',['foreign_owner','different_model','different_path','education_claim','foreign_parent'])
def test_variant_delete_rechecks_identity_and_protection_in_final_write(variants_client,review_db,monkeypatch,change):
 from sqlalchemy.orm import Session
 client,issue=variants_client;execute=Session.execute;seen=[]
 def changing(self,statement,*args,**kwargs):
  if str(statement).startswith('DELETE FROM print_files') and not seen:
   seen.append(change)
   if change=='education_claim':
    execute(self,text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('synthetic-new-claim',1,1,'reserved','/opaque/variant-201.3mf')"))
   elif change=='foreign_parent':
    execute(self,text('UPDATE models SET org_id=2 WHERE id=101'))
   else:
    column,value={'foreign_owner':('org_id',2),'different_model':('model_id',103),'different_path':('stored_path','/opaque/new-owned-path.3mf')}[change]
    execute(self,text(f'UPDATE print_files SET {column}=:value WHERE id=201'),{'value':value})
   self.commit()
  return execute(self,statement,*args,**kwargs)
 monkeypatch.setattr(Session,'execute',changing)
 response=client.delete('/models/101/variants/201',headers=issue(['write:models']))
 assert response.status_code==404,response.text
 assert seen==[change]
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=201')).scalar_one()==1


def test_priced_model_list_counts_and_dimensions_only_visible_variants(variants_client,review_db):
 client,issue=variants_client
 review_db.execute(text("UPDATE print_files SET bed_x_mm=256,bed_y_mm=256,uploaded_at='2026-10-01' WHERE id=201"))
 review_db.execute(text("UPDATE print_files SET bed_x_mm=999,bed_y_mm=888,uploaded_at='2026-10-11' WHERE id=202"));review_db.commit()
 response=client.get('/models-with-pricing',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 row=next(row for row in response.json() if row['id']==101)
 assert row['variant_count']==2
 assert row['bed_x_mm']==256 and row['bed_y_mm']==256


def test_priced_model_list_uses_zero_visible_variants_when_private(variants_client,review_db):
 client,issue=variants_client;headers=issue(['read:models'])
 review_db.execute(text('UPDATE api_tokens SET user_id=4 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 response=client.get('/models-with-pricing',headers=headers)
 assert response.status_code==200,response.text
 row=next(row for row in response.json() if row['id']==12)
 assert row['variant_count']==0
 assert row['bed_x_mm'] is None and row['bed_y_mm'] is None
