"""Real HTTP/DB acceptance for multi-printer jobs; never dispatch hardware."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def batch_context(reprint_client,review_db):
 client,identity,_=reprint_client
 review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,is_active) VALUES(12,'Synthetic own second','bambu',1,TRUE)"))
 review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(103,'Synthetic private foreign model',2)"))
 review_db.commit()
 return client,identity,review_db

def body(printer_ids):
 return {'item_name':'Synthetic batch','model_id':101,'printer_ids':printer_ids,'priority':2,'duration_hours':1.5,'filament_type':'PETG','colors_required':'#112233','notes':'Synthetic exact fields','queue_only':True}

def test_batch_creates_exact_distinct_jobs_with_owner_and_requested_fields(batch_context):
 client,identity,db=batch_context
 before=db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/jobs/batch',json=body([9,12]))
 assert response.status_code==200,response.text
 rows=response.json();assert len(rows)==2 and {r['printer_id'] for r in rows}=={9,12}
 assert len({r['id'] for r in rows})==2
 db.expire_all()
 assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before+2
 for row in rows:
  saved=db.execute(text('SELECT model_id,status,priority,duration_hours,filament_type,colors_required,hold,charged_to_org_id,charged_to_user_id,submitted_by,notes FROM jobs WHERE id=:id'),{'id':row['id']}).one()
  assert tuple(saved)==(101,'pending',2,1.5,'PETG','#112233',1,1,3,3,'Synthetic exact fields')

@pytest.mark.parametrize('target',[[],[9,11],[9,999999],[9,9]])
def test_invalid_or_duplicate_batch_targets_leave_zero_new_jobs(batch_context,target):
 client,identity,db=batch_context
 before=db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=client.post('/jobs/batch',json=body(target))
 assert response.status_code in (400,422),response.text
 db.expire_all();assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before

@pytest.mark.parametrize("model_id", [103, 999999])
def test_foreign_model_batch_is_denied_before_pricing_or_persistence(batch_context,monkeypatch,model_id):
 from modules.models_library import services
 client,identity,db=batch_context
 calls=[]
 original=services.calculate_job_cost
 def pricing(*args,**kwargs):calls.append(kwargs.get('model_id'));return original(*args,**kwargs)
 monkeypatch.setattr(services,'calculate_job_cost',pricing)
 before=db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one();request=body([9]);request['model_id']=model_id
 response=client.post('/jobs/batch',json=request)
 assert response.status_code==404,response.text
 assert calls==[]
 db.expire_all();assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before

@pytest.mark.parametrize('role,scopes',[('viewer',None),('operator',['read:jobs']),('operator',[])])
def test_batch_denied_roles_and_tokens_leave_zero_new_jobs(batch_context,role,scopes):
 client,identity,db=batch_context;identity['role']=role
 if scopes is not None:identity.update(_auth_kind='user_api_token',_token_scopes=scopes)
 before=db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 assert client.post('/jobs/batch',json=body([9,12])).status_code==403
 db.expire_all();assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before

def test_queue_only_batch_stays_pending_when_actual_scheduler_runs(batch_context):
 from modules.jobs.scheduler import Scheduler
 from datetime import datetime,timezone,timedelta
 client,identity,db=batch_context
 response=client.post('/jobs/batch',json=body([9,12]));assert response.status_code==200,response.text
 identifiers=[row['id'] for row in response.json()]
 Scheduler().run(db,start_date=datetime.now(timezone.utc)+timedelta(days=1));db.commit();db.expire_all()
 for identifier in identifiers:
  assert tuple(db.execute(text('SELECT status,hold,scheduled_start,scheduled_end FROM jobs WHERE id=:id'),{'id':identifier}).one())==('pending',1,None,None)

@pytest.mark.parametrize('model_id,global_model,superadmin',[(None,False,False),(101,False,False),(103,True,False),(103,False,True)])
def test_batch_optional_owned_global_and_superadmin_models(batch_context,model_id,global_model,superadmin):
 client,identity,db=batch_context
 if global_model:db.execute(text('UPDATE models SET org_id=NULL WHERE id=103'));db.commit()
 if superadmin:identity.update(role='admin',group_id=None)
 request=body([9]);request['model_id']=model_id
 response=client.post('/jobs/batch',json=request)
 assert response.status_code==200,response.text
 identifier=response.json()[0]['id']
 assert tuple(db.execute(text('SELECT model_id,submitted_by,status FROM jobs WHERE id=:id'),{'id':identifier}).one())==(model_id,3,'pending')

@pytest.mark.parametrize('scopes',[['write'],['write:jobs']])
def test_batch_authorized_token_can_create(batch_context,scopes):
 client,identity,db=batch_context;identity.update(_auth_kind='user_api_token',_token_scopes=scopes)
 response=client.post('/jobs/batch',json=body([9]))
 assert response.status_code==200,response.text
 assert db.execute(text('SELECT submitted_by FROM jobs WHERE id=:id'),{'id':response.json()[0]['id']}).scalar_one()==3

@pytest.mark.parametrize('model_id,targets,status',[(101,[9,11],200),(103,[11],404),(101,[11,11],400)])
def test_shared_printer_does_not_grant_foreign_model_or_duplicate_targets(batch_context,model_id,targets,status):
 client,identity,db=batch_context
 db.execute(text('UPDATE printers SET shared=TRUE WHERE id=11'));db.commit()
 before=db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 request=body(targets);request['model_id']=model_id
 response=client.post('/jobs/batch',json=request)
 assert response.status_code==status,response.text
 assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before+(len(targets) if status==200 else 0)
