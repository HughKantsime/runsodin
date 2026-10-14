"""Actual generic file CRUD/scheduling respects private files and full graph integrity."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def managed_files(token_client,review_db,monkeypatch,tmp_path):
 from modules.models_library.routes import print_files
 from core.registry import registry
 from modules.organizations.services import EducationPolicyService
 from modules.jobs import register_providers
 monkeypatch.setattr(registry, '_providers', dict(registry._providers))
 register_providers(registry)
 client,issue=token_client;client.app.include_router(print_files.router)
 monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
 for identifier,org in [(21,1),(22,2),(23,None)]:
  source=tmp_path/f'owned-file-{identifier}.gcode';source.write_text('; owned audience fixture')
  review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(:id,:name,:org)"),{'id':identifier+10,'name':f'Synthetic model {identifier}','org':org})
  review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,project_name,stored_path,model_id,org_id,print_time_seconds,filaments_json) VALUES(:id,:name,:name,:name,:path,:model,:org,60,'[]')"),{'id':identifier,'name':f'Synthetic private file {identifier}','path':str(source),'model':identifier+10,'org':org})
 review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9"))
 review_db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
 review_db.execute(text("INSERT INTO printers(id,name,model,api_type,org_id,is_active) VALUES(19,'Synthetic foreign printer','Synthetic','moonraker',2,1)"));review_db.commit()
 return TestClient(client.app,raise_server_exceptions=False),issue

@pytest.mark.parametrize('scopes',[['read:printers'],['write:jobs']])
@pytest.mark.parametrize('path',['/print-files','/print-files/21'])
def test_unrelated_read_scope_cannot_read_file_metadata(managed_files,scopes,path):
 client,issue=managed_files
 response=client.get(path,headers=issue(scopes))
 assert response.status_code==403,response.text


def test_file_list_applies_audience_before_limit_and_hides_foreign_name(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text("UPDATE print_files SET uploaded_at='2026-10-01'"))
 review_db.execute(text("UPDATE print_files SET uploaded_at='2026-10-11' WHERE id=22"))
 review_db.execute(text("UPDATE print_files SET uploaded_at='2026-10-10' WHERE id=21"));review_db.commit()
 response=client.get('/print-files?limit=1',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 assert [row['id'] for row in response.json()]==[21]
 assert 'Synthetic private file 22' not in response.text

@pytest.mark.parametrize('method,path,scopes',[('get','/print-files/22',['read:models']),('delete','/print-files/22',['write:models']),('post','/print-files/22/schedule',['write:jobs'])])
def test_foreign_file_operations_are_opaque_and_leave_rows_intact(managed_files,review_db,method,path,scopes):
 client,issue=managed_files;before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=getattr(client,method)(path,headers=issue(scopes))
 assert response.status_code==404,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=22')).scalar_one()==1
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before

@pytest.mark.parametrize('scopes',[['read:models'],['write:printers'],['write:jobs']])
def test_unrelated_delete_scope_preserves_file(managed_files,review_db,scopes):
 client,issue=managed_files
 response=client.delete('/print-files/21',headers=issue(scopes))
 assert response.status_code==403,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=21')).scalar_one()==1

@pytest.mark.parametrize('scopes',[['read:models'],['write:printers'],['write:models']])
def test_unrelated_schedule_scope_preserves_job_graph(managed_files,review_db,scopes):
 client,issue=managed_files;before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/print-files/21/schedule',headers=issue(scopes))
 assert response.status_code==403,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before


def test_other_student_cannot_read_private_education_file_or_list_entry(managed_files,review_db):
 client,issue=managed_files;headers=issue(['read:models'])
 review_db.execute(text('UPDATE api_tokens SET user_id=4 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 detail=client.get('/print-files/11',headers=headers)
 assert detail.status_code==404
 response=client.get('/print-files?include_scheduled=true',headers=headers)
 assert response.status_code==200
 assert 11 not in {row['id'] for row in response.json()}

@pytest.mark.parametrize('operation',['delete','schedule'])
def test_generic_education_mutation_denied_even_when_authority_absent(managed_files,review_db,monkeypatch,operation):
 from core.registry import registry
 client,issue=managed_files;monkeypatch.setitem(registry._providers,'EducationPolicyProvider',None)
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 path='/print-files/11' if operation=='delete' else '/print-files/11/schedule'
 response=client.delete(path,headers=issue(['write:models'])) if operation=='delete' else client.post(path,headers=issue(['write:jobs']))
 assert response.status_code in (404,409),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=11')).scalar_one()==1
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before


def test_scheduling_foreign_printer_is_opaque_before_job_creation(managed_files,review_db):
 client,issue=managed_files;before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/print-files/21/schedule?printer_id=19',headers=issue(['write:jobs']))
 assert response.status_code==404,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before


def test_file_schedule_persists_actor_model_charge_and_explicit_file_link(managed_files,review_db):
 client,issue=managed_files
 response=client.post('/print-files/21/schedule?printer_id=9',headers=issue(['write:jobs']))
 assert response.status_code==200,response.text
 identifier=response.json()['job_id']
 row=review_db.execute(text('SELECT model_id,charged_to_org_id,charged_to_user_id,submitted_by,printer_id FROM jobs WHERE id=:id'),{'id':identifier}).one()
 assert tuple(row)==(31,1,3,3,9)
 assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one()==identifier


def test_file_link_failure_does_not_leave_committed_schedule_job(managed_files,review_db,monkeypatch):
 from sqlalchemy.orm import Session
 client,issue=managed_files;execute=Session.execute
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 def refused(self,statement,*args,**kwargs):
  if str(statement).startswith('UPDATE print_files SET job_id'):raise RuntimeError('Synthetic file-link failure')
  return execute(self,statement,*args,**kwargs)
 monkeypatch.setattr(Session,'execute',refused)
 response=client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code==500
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before
 assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one() is None

@pytest.mark.parametrize('file_id',[21,23])
@pytest.mark.parametrize('scope',['read:models','read'])
def test_authorized_owned_or_global_detail_retains_metadata_without_server_path(managed_files,file_id,scope):
 client,issue=managed_files
 response=client.get(f'/print-files/{file_id}',headers=issue([scope]))
 assert response.status_code==200,response.text
 assert response.json()['id']==file_id
 assert response.json()['original_filename']==f'Synthetic private file {file_id}'
 assert 'stored_path' not in response.json()
 assert response.json()['print_time_formatted'] in ('1m','1.0m')

@pytest.mark.parametrize('file_id',[21,23])
def test_authorized_owned_or_global_delete_removes_only_requested_record(managed_files,review_db,file_id):
 client,issue=managed_files
 response=client.delete(f'/print-files/{file_id}',headers=issue(['write:models']))
 assert response.status_code==200,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=:id'),{'id':file_id}).scalar_one()==0
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=22')).scalar_one()==1


def test_unknown_print_duration_detail_is_not_a_server_error(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text('UPDATE print_files SET print_time_seconds=NULL WHERE id=21'));review_db.commit()
 response=client.get('/print-files/21',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 assert response.json()['print_time_seconds'] is None
 assert response.json()['print_time_formatted'] is None


def test_repeated_schedule_does_not_create_second_job(managed_files,review_db):
 client,issue=managed_files;headers=issue(['write:jobs'])
 first=client.post('/print-files/21/schedule',headers=headers)
 assert first.status_code==200,first.text
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 second=client.post('/print-files/21/schedule',headers=headers)
 assert second.status_code in (400,409),second.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before
 assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one()==first.json()['job_id']

@pytest.mark.parametrize('operation',['delete','schedule'])
@pytest.mark.parametrize('state',['reserved','streaming','staged','renamed','committed','aborted','needs_reconciliation','purge_pending','purged'])
def test_operation_path_alias_cannot_bypass_education_mutation_protection(managed_files,review_db,operation,state):
 client,issue=managed_files
 review_db.execute(text('UPDATE print_files SET stored_path=\'/opaque/upload-1.3mf\' WHERE id=21'))
 review_db.execute(text('UPDATE education_upload_operations SET state=:state WHERE operation_id=\'upload-1\''),{'state':state});review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.delete('/print-files/21',headers=issue(['write:models'])) if operation=='delete' else client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code in (404,409),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=21')).scalar_one()==1
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before

@pytest.mark.parametrize('model_id',[32,99999,12])
def test_schedule_rejects_foreign_missing_or_education_parent_before_creating_job(managed_files,review_db,model_id):
 client,issue=managed_files
 # The raw print-file reference has no FK, so malformed legacy associations are representable.
 review_db.execute(text('UPDATE print_files SET model_id=:id WHERE id=21'),{'id':model_id});review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code in (404,409),response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before


def test_scheduled_job_keeps_exact_file_when_model_original_differs(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text('UPDATE models SET print_file_id=23 WHERE id=31'));review_db.commit()
 response=client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code==200,response.text
 row=review_db.execute(text('SELECT model_id,print_file_id FROM jobs WHERE id=:id'),{'id':response.json()['job_id']}).one()
 assert tuple(row)==(31,21)

@pytest.mark.parametrize('printer_id',[None,19])
def test_optional_or_shared_printer_is_an_authorized_target(managed_files,review_db,printer_id):
 client,issue=managed_files
 if printer_id is not None:
  review_db.execute(text('UPDATE printers SET shared=1 WHERE id=19'));review_db.commit()
 suffix='' if printer_id is None else '?printer_id=19'
 response=client.post('/print-files/21/schedule'+suffix,headers=issue(['write:jobs']))
 assert response.status_code==200,response.text
 assert review_db.execute(text('SELECT printer_id FROM jobs WHERE id=:id'),{'id':response.json()['job_id']}).scalar_one()==printer_id


def test_viewer_cannot_bypass_approval_using_generic_file_schedule(managed_files,review_db):
 client,issue=managed_files;headers=issue(['write:jobs'])
 review_db.execute(text('UPDATE api_tokens SET user_id=1 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/print-files/21/schedule',headers=headers)
 assert response.status_code==403,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before


def test_operator_schedule_preserves_existing_approval_policy(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
 review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_job_approval','true')"));review_db.commit()
 response=client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code==200,response.text
 # Existing generic approval requires approval for viewers; operators may enqueue.
 assert review_db.execute(text('SELECT status,submitted_by FROM jobs WHERE id=:id'),{'id':response.json()['job_id']}).one()==('pending',3)

@pytest.mark.parametrize('explicit_link',[False,True])
def test_delete_preserves_files_referenced_by_pending_jobs(managed_files,review_db,explicit_link):
 client,issue=managed_files
 review_db.execute(text("INSERT INTO jobs(id,item_name,status,model_id,print_file_id,charged_to_org_id) VALUES(41,'Synthetic queued upload','pending',31,:file,1)"),{'file':21 if explicit_link else None})
 if not explicit_link:review_db.execute(text('UPDATE print_files SET job_id=41 WHERE id=21'))
 review_db.commit()
 response=client.delete('/print-files/21',headers=issue(['write:models']))
 assert response.status_code==409,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=21')).scalar_one()==1
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=41')).scalar_one()==1


def test_model_less_generic_file_can_be_deleted(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text('UPDATE print_files SET model_id=NULL WHERE id=21'));review_db.commit()
 response=client.delete('/print-files/21',headers=issue(['write:models']))
 assert response.status_code==200,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=21')).scalar_one()==0

@pytest.mark.parametrize('job_org,job_model,job_file',[(2,31,21),(1,32,21),(1,31,22)])
def test_file_list_does_not_disclose_foreign_or_inconsistent_joined_job(managed_files,review_db,job_org,job_model,job_file):
 client,issue=managed_files
 review_db.execute(text("INSERT INTO jobs(id,item_name,status,model_id,print_file_id,charged_to_org_id) VALUES(41,'Synthetic private joined job','pending',:model,:file,:org)"),{'model':job_model,'file':job_file,'org':job_org})
 review_db.execute(text('UPDATE print_files SET job_id=41 WHERE id=21'));review_db.commit()
 response=client.get('/print-files?include_scheduled=true',headers=issue(['read:models']))
 assert response.status_code==200,response.text
 row=next(row for row in response.json() if row['id']==21)
 assert row.get('job_name') is None and row.get('job_status') is None
 assert 'Synthetic private joined job' not in response.text


def test_authorized_education_file_cannot_leak_a_different_submissions_job(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,expected_hash,final_path) VALUES('synthetic-other',1,4,'committed',0,0,0,'synthetic','/opaque/synthetic-other.3mf')"))
 review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id,created_by,model_id) VALUES(44,'Synthetic other.3mf','Synthetic other.3mf','/opaque/synthetic-other.3mf',1,4,55)"))
 review_db.execute(text("INSERT INTO models(id,name,org_id,print_file_id) VALUES(55,'Synthetic other model',1,44)"))
 review_db.execute(text("INSERT INTO jobs(id,item_name,status,model_id,print_file_id,submitted_by,charged_to_org_id,charged_to_user_id) VALUES(99,'Synthetic other students secret job','submitted',12,11,4,1,4)"))
 review_db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision) VALUES(45,1,'synthetic-other',99,44,55,7,4,'submitted',1)"))
 review_db.execute(text('UPDATE print_files SET job_id=99 WHERE id=11'));review_db.commit()
 headers=issue(['read:models'])
 review_db.execute(text('UPDATE api_tokens SET user_id=1 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 response=client.get('/print-files?include_scheduled=true',headers=headers)
 assert response.status_code==200,response.text
 row=next(row for row in response.json() if row['id']==11)
 assert row.get('job_name') is None and row.get('job_status') is None
 assert 'Synthetic other students secret job' not in response.text

@pytest.mark.parametrize('operation,change',[('delete','job_reference'),('delete','foreign_owner'),('schedule','foreign_owner'),('schedule','education_primary_file'),('schedule','unshared_printer')])
def test_identity_and_lifecycle_changes_at_final_write_preserve_file_and_rollback_job(managed_files,review_db,monkeypatch,operation,change):
 from sqlalchemy.orm import Session
 client,issue=managed_files;execute=Session.execute;seen=[]
 if change=='unshared_printer':
  review_db.execute(text('UPDATE printers SET shared=1 WHERE id=19'));review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 def changing(self,statement,*args,**kwargs):
  sql=str(statement)
  relevant=sql.startswith('DELETE FROM print_files') if operation=='delete' else sql.startswith('UPDATE print_files SET job_id')
  if relevant and not seen:
   seen.append(change)
   if change=='job_reference':
    execute(self,text("INSERT INTO jobs(id,item_name,status,print_file_id) VALUES(41,'Synthetic new reference','pending',21)"))
   elif change=='foreign_owner':execute(self,text('UPDATE print_files SET org_id=2 WHERE id=21'))
   elif change=='education_primary_file':execute(self,text('UPDATE models SET print_file_id=11 WHERE id=31'))
   elif change=='unshared_printer':execute(self,text('UPDATE printers SET shared=0 WHERE id=19'))
   # For deletion, represent an independently committed pre-write change. Scheduling
   # holds the file write lock, so in-transaction graph changes test its final predicate.
   if operation=='delete':self.commit()
  return execute(self,statement,*args,**kwargs)
 monkeypatch.setattr(Session,'execute',changing)
 response=client.delete('/print-files/21',headers=issue(['write:models'])) if operation=='delete' else client.post('/print-files/21/schedule?printer_id='+('19' if change=='unshared_printer' else '9'),headers=issue(['write:jobs']))
 assert response.status_code in (404,409),response.text
 assert seen==[change]
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=21')).scalar_one()==1
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before+(1 if change=='job_reference' else 0)
 assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one() is None


def test_missing_stored_payload_is_not_scheduled(managed_files,review_db):
 client,issue=managed_files
 review_db.execute(text('UPDATE print_files SET stored_path=NULL WHERE id=21'));review_db.commit()
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code==400,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before

@pytest.mark.parametrize('operation',['delete','schedule'])
def test_actual_audit_flush_failure_rolls_back_generic_file_mutation(managed_files,review_db,monkeypatch,operation):
 from modules.models_library import file_management
 from core.models import AuditLog
 client,issue=managed_files
 def invalid_audit(db,*args,**kwargs):db.add(AuditLog(action=None,entity_type='print_file',entity_id=21))
 monkeypatch.setattr(file_management,'log_audit',invalid_audit)
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.delete('/print-files/21',headers=issue(['write:models'])) if operation=='delete' else client.post('/print-files/21/schedule',headers=issue(['write:jobs']))
 assert response.status_code==500,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=21')).scalar_one()==1
 assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one() is None
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before
 assert 'NOT NULL' not in response.text and 'INSERT INTO' not in response.text


def test_commit_error_after_success_does_not_allow_duplicate_scheduling(managed_files,review_db,monkeypatch):
 from sqlalchemy.orm import Session
 client,issue=managed_files;headers=issue(['write:jobs']);commit=Session.commit;seen=[]
 def uncertain(self):
  commit(self)
  if not seen and self.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one() is not None:
   seen.append(True);raise RuntimeError('PRIVATE_SYNTHETIC_DATABASE_DETAIL')
 monkeypatch.setattr(Session,'commit',uncertain)
 response=client.post('/print-files/21/schedule',headers=headers)
 assert response.status_code==500,response.text
 assert 'PRIVATE_SYNTHETIC_DATABASE_DETAIL' not in response.text
 identifier=review_db.execute(text('SELECT job_id FROM print_files WHERE id=21')).scalar_one()
 assert identifier is not None
 before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 repeated=client.post('/print-files/21/schedule',headers=headers)
 assert repeated.status_code==409,repeated.text
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before
