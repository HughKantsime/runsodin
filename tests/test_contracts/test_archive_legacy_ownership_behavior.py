"""Bounded explicit legacy ownership reconciliation through real HTTP and DB."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def legacy_archive(reprint_client,review_db):
 client,identity,_=reprint_client
 review_db.execute(text("UPDATE print_files SET org_id=1 WHERE id=101"))
 review_db.execute(text("UPDATE print_archives SET org_id=NULL,ownership_state=NULL,ownership_source=NULL WHERE id=101"))
 review_db.execute(text('UPDATE printers SET shared=TRUE WHERE id=9'));review_db.commit()
 identity.update(id=3,role='admin',group_id=None)
 return client,identity,review_db


def test_explicit_legacy_reconciliation_restores_only_real_content_owner(legacy_archive):
 client,identity,db=legacy_archive
 identity.update(group_id=1)
 assert client.get('/archives/101').status_code==404
 identity.update(group_id=None)
 response=client.post('/archives/reconcile-ownership',json={'limit':10})
 assert response.status_code==200,response.text
 assert response.json()['reconciled']==1 and response.json()['pending_count']==0
 identity.update(group_id=1)
 assert client.get('/archives/101').status_code==200
 identity.update(id=5,group_id=2)
 assert client.get('/archives/101').status_code==404
 db.expire_all()
 assert tuple(db.execute(text('SELECT org_id,ownership_state,ownership_source FROM print_archives WHERE id=101')).one())==(1,'known','legacy_job_file')
 assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one()==0
 assert db.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox')).scalar_one()==0


@pytest.mark.parametrize('role,org,scopes,expected',[
 ('admin',1,None,403),('operator',None,None,403),('admin',None,['read:jobs'],403),
 ('admin',None,[],403),('admin',None,['write:jobs'],200),
])
def test_reconciliation_requires_superadmin_and_write_action(legacy_archive,role,org,scopes,expected):
 client,identity,db=legacy_archive
 identity.update(role=role,group_id=org)
 if scopes is not None:identity.update(_auth_kind='user_api_token',_token_scopes=scopes)
 response=client.post('/archives/reconcile-ownership',json={})
 assert response.status_code==expected,response.text
 db.expire_all()
 assert db.execute(text('SELECT ownership_state FROM print_archives WHERE id=101')).scalar_one()==('known' if expected==200 else None)


@pytest.mark.parametrize('body',[{'limit':0},{'limit':101},{'limit':True},{'after_archive_id':-1},{'after_archive_id':True}])
def test_reconciliation_rejects_invalid_batch_parameters_without_mutation(legacy_archive,body):
 client,identity,db=legacy_archive
 assert client.post('/archives/reconcile-ownership',json=body).status_code==422
 assert db.execute(text('SELECT ownership_state FROM print_archives WHERE id=101')).scalar_one() is None


def test_known_anchor_is_immutable_and_repeat_is_noop(legacy_archive):
 client,identity,db=legacy_archive
 assert client.post('/archives/reconcile-ownership',json={}).status_code==200
 db.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=101'))
 db.execute(text('UPDATE print_files SET org_id=2 WHERE id=101'));db.commit()
 response=client.post('/archives/reconcile-ownership',json={})
 assert response.status_code==200 and response.json()['reconciled']==0
 db.expire_all()
 assert tuple(db.execute(text('SELECT org_id,ownership_state FROM print_archives WHERE id=101')).one())==(1,'known')


@pytest.mark.parametrize('problem',['missing_file_id','missing_job','foreign_file','sticky_conflict'])
def test_unproven_or_conflicting_history_stays_unresolved(legacy_archive,problem):
 client,identity,db=legacy_archive
 if problem=='missing_file_id':db.execute(text('UPDATE print_archives SET print_file_id=NULL WHERE id=101'))
 elif problem=='missing_job':db.execute(text('UPDATE print_archives SET job_id=NULL WHERE id=101'))
 elif problem=='foreign_file':db.execute(text('UPDATE print_files SET org_id=2 WHERE id=101'))
 else:db.execute(text("UPDATE print_archives SET org_id=1,ownership_state='unresolved',ownership_source='source_graph_changed' WHERE id=101"))
 db.commit()
 response=client.post('/archives/reconcile-ownership',json={})
 assert response.status_code==200,response.text
 assert response.json()['reconciled']==0 and response.json()['pending_count']==1
 identity.update(group_id=1)
 assert client.get('/archives/101').status_code==404


def test_cursor_progresses_past_unresolved_without_discarding_it(legacy_archive):
 client,identity,db=legacy_archive
 db.execute(text("INSERT INTO print_archives(id,print_name,status) VALUES(100,'Unattributed older','completed')"));db.commit()
 first=client.post('/archives/reconcile-ownership',json={'limit':1}).json()
 assert first['attempted']==1 and first['reconciled']==0 and first['next_after_id']==100
 second=client.post('/archives/reconcile-ownership',json={'limit':1,'after_archive_id':first['next_after_id']}).json()
 assert second['reconciled']==1 and second['next_after_id']==101 and second['pending_count']==1
 db.expire_all()
 assert db.execute(text('SELECT ownership_state FROM print_archives WHERE id=100')).scalar_one() is None


def test_batch_failure_rolls_back_every_ownership_transition(legacy_archive):
 client,identity,db=legacy_archive
 db.execute(text("INSERT INTO print_archives(id,job_id,print_file_id,print_name,status) VALUES(102,101,101,'Second legacy','completed')"))
 db.execute(text("CREATE TRIGGER reject_second_owner BEFORE UPDATE OF ownership_state ON print_archives WHEN NEW.id=102 BEGIN SELECT RAISE(ABORT,'synthetic ownership persistence failure'); END"));db.commit()
 response=client.post('/archives/reconcile-ownership',json={'limit':10})
 assert response.status_code==500
 db.expire_all()
 assert db.execute(text('SELECT COUNT(*) FROM print_archives WHERE ownership_state IS NOT NULL')).scalar_one()==0


@pytest.mark.parametrize('problem',[None,'file_creator','model_file','archived_submitter'])
def test_legacy_education_requires_complete_source_graph(legacy_archive,problem):
 client,identity,db=legacy_archive
 db.execute(text('UPDATE jobs SET print_file_id=11 WHERE id=13'))
 db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
 db.execute(text('UPDATE print_archives SET job_id=13,print_file_id=11,file_path=NULL,user_id=1,education_submission_id=14 WHERE id=101'))
 if problem=='file_creator':db.execute(text('UPDATE print_files SET created_by=4 WHERE id=11'))
 elif problem=='model_file':db.execute(text('UPDATE models SET print_file_id=101 WHERE id=12'))
 elif problem=='archived_submitter':db.execute(text('UPDATE print_archives SET user_id=4 WHERE id=101'))
 db.commit()
 response=client.post('/archives/reconcile-ownership',json={})
 assert response.status_code==200,response.text
 assert response.json()['reconciled']==(1 if problem is None else 0)
 identity.update(id=1,role='viewer',group_id=1)
 assert client.get('/archives/101').status_code==(200 if problem is None else 404)
 identity.update(id=4)
 assert client.get('/archives/101').status_code==404


@pytest.mark.parametrize('capture',['match','foreign_owner','wrong_attempt_job','old_plan_conflict'])
def test_legacy_reconciliation_respects_retained_physical_attempt_evidence(legacy_archive,capture):
 import json
 client,identity,db=legacy_archive
 owner={'state':'known','org_id':1,'scheduled_job_id':101,'print_file_id':101,'education_submission_id':None}
 if capture=='foreign_owner':owner['org_id']=2
 snapshot={'version':1,'content_owner':owner}
 if capture=='old_plan_conflict':snapshot={'version':1,'material_plan':{'scheduled_job_id':101,'print_file_id':101,'org_id':2,'state':'complete'}}
 db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,spool_snapshot,started_at) VALUES(201,9,:job,'completed',:snapshot,CURRENT_TIMESTAMP)"),{'job':None if capture=='wrong_attempt_job' else 101,'snapshot':json.dumps(snapshot)})
 db.execute(text('UPDATE print_archives SET print_job_id=201 WHERE id=101'));db.commit()
 response=client.post('/archives/reconcile-ownership',json={})
 assert response.status_code==200,response.text
 assert response.json()['reconciled']==(1 if capture=='match' else 0)
 if capture=='match':
  db.expire_all()
  assert db.execute(text('SELECT ownership_source FROM print_archives WHERE id=101')).scalar_one()=='legacy_first_start'


def test_retained_education_marker_cannot_be_cleared_by_missing_sources(legacy_archive):
 client,identity,db=legacy_archive
 db.execute(text('UPDATE print_archives SET education_submission_id=-1 WHERE id=101'));db.commit()
 result=client.post('/archives/reconcile-ownership',json={}).json()
 assert result['reconciled']==0 and result['pending_count']==1
 db.expire_all()
 assert db.execute(text('SELECT education_submission_id FROM print_archives WHERE id=101')).scalar_one()==-1


@pytest.mark.parametrize('sqlstate,expected',[('55P03',409),('40P01',500),('23503',500),('08006',500)])
def test_reconciliation_contention_is_sanitized_and_rolls_back_caller_transaction(legacy_archive,monkeypatch,sqlstate,expected):
 from sqlalchemy.exc import OperationalError
 from modules.archives import legacy_ownership
 client,identity,db=legacy_archive
 class SyntheticDatabaseFailure(Exception):
  pass
 failure=SyntheticDatabaseFailure('private synthetic database detail')
 failure.sqlstate=sqlstate
 def persistence_failure(executor,**kwargs):
  executor.execute(text("UPDATE print_archives SET org_id=1,ownership_state='known' WHERE id=101"))
  raise OperationalError('private synthetic SQL',{},failure)
 monkeypatch.setattr(legacy_ownership,'resolve_legacy_archive_ownership',persistence_failure)
 response=client.post('/archives/reconcile-ownership',json={})
 assert response.status_code==expected
 assert 'private synthetic' not in response.text
 if expected==409:
  assert response.json()=={'detail':'Archive repair is temporarily busy. Retry after current activity finishes.'}
 db.expire_all()
 assert tuple(db.execute(text('SELECT org_id,ownership_state FROM print_archives WHERE id=101')).one())==(None,None)
 assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one()==0
 assert db.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox')).scalar_one()==0
