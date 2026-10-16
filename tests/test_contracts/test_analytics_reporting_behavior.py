"""Known-value failure reporting checks, synthetic isolated HTTP inputs."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_analytics_financial_behavior import analytics_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")


def test_hms_same_code_preserves_distinct_messages(analytics_client, review_db):
    review_db.execute(text("INSERT INTO hms_error_history(printer_id,code,message,occurred_at) VALUES(9,'SYNTHETIC','Synthetic first',CURRENT_TIMESTAMP),(9,'SYNTHETIC','Synthetic second',CURRENT_TIMESTAMP),(9,'SYNTHETIC','Synthetic first',CURRENT_TIMESTAMP)"))
    review_db.commit()
    response=analytics_client.get('/analytics/failures')
    assert response.status_code==200
    rows=[row for row in response.json()['hms_errors'] if row['code']=='SYNTHETIC']
    assert {(row['message'],row['count']) for row in rows}=={('Synthetic first',2),('Synthetic second',1)}


def test_failure_analytics_does_not_expose_foreign_related_names(analytics_client, review_db):
    review_db.execute(text("INSERT INTO printers(id,name,model,org_id,shared) VALUES(99,'Synthetic private foreign printer','Synthetic',2,0)"))
    review_db.execute(text("UPDATE models SET name='Synthetic private foreign model' WHERE id=102"))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,model_id,charged_to_org_id,created_at) VALUES('Synthetic visible job','failed',1,99,102,1,CURRENT_TIMESTAMP),('Synthetic visible job','completed',1,99,102,1,CURRENT_TIMESTAMP)"))
    review_db.commit()
    response=analytics_client.get('/analytics/failures')
    assert response.status_code==200
    assert 'Synthetic private foreign' not in response.text
    assert response.json()['total_failed']>=1


@pytest.mark.parametrize('scope',['own','shared','unscoped','foreign','superadmin'])
def test_failure_related_printer_access_and_hms_match_policy(analytics_client,review_db,scope):
    from core.dependencies import get_current_user
    org=None if scope=='unscoped' else (1 if scope=='own' else 2)
    shared=scope=='shared'
    review_db.execute(text("INSERT INTO printers(id,name,model,org_id,shared) VALUES(99,'Synthetic access printer','Synthetic',:org,:shared)"),{'org':org,'shared':shared})
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,charged_to_org_id,created_at) VALUES('Synthetic access job','failed',1,99,1,CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO hms_error_history(printer_id,code,message,occurred_at) VALUES(99,'SYNTHETIC-ACCESS','Synthetic access error',CURRENT_TIMESTAMP)"))
    review_db.commit()
    if scope=='superadmin':
        analytics_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'is_active':True}
    response=analytics_client.get('/analytics/failures')
    assert response.status_code==200
    allowed=scope!='foreign'
    assert ('Synthetic access printer' in response.text)==allowed
    assert ('Synthetic access error' in response.text)==allowed
    assert response.json()['total_failed']>=1


@pytest.mark.parametrize('scope',['own','unscoped','foreign','superadmin'])
def test_failure_model_groups_match_tenant_access(analytics_client,review_db,scope):
    from core.dependencies import get_current_user
    org=None if scope=='unscoped' else (1 if scope=='own' else 2)
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(199,'Synthetic access model',:org)"),{'org':org})
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,model_id,charged_to_org_id,created_at) VALUES('Synthetic visible model job','failed',1,199,1,CURRENT_TIMESTAMP),('Synthetic visible model job','completed',1,199,1,CURRENT_TIMESTAMP)"))
    review_db.commit()
    if scope=='superadmin':
        analytics_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'is_active':True}
    response=analytics_client.get('/analytics/failures')
    assert response.status_code==200
    rows=[r for r in response.json()['by_model'] if r['name']=='Synthetic access model']
    if scope=='foreign':assert rows==[]
    else:assert rows==[{'name':'Synthetic access model','completed':1,'failed':1,'success_rate':50.0}]



def test_time_accuracy_uses_independent_scheduled_interval_not_overwritten_duration(analytics_client,review_db):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    start=end-timedelta(hours=3)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,charged_to_org_id,actual_start,actual_end,scheduled_start,scheduled_end) VALUES('Synthetic timed job','completed',2,3,1,:start,:end,:start,:scheduled_end)"),{'start':start,'end':end,'scheduled_end':start+timedelta(hours=2)})
    review_db.commit()
    response=analytics_client.get('/analytics/time-accuracy')
    assert response.status_code==200
    row=response.json()['recent'][0]
    assert row['estimated_hours']==2 and row['actual_hours']==3 and row['accuracy_pct']==66.7


def test_time_accuracy_does_not_invent_legacy_estimate(analytics_client,review_db):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,charged_to_org_id,actual_start,actual_end) VALUES('Synthetic legacy actual only','completed',1,3,1,:start,:end)"),{'start':end-timedelta(hours=3),'end':end})
    review_db.commit()
    response=analytics_client.get('/analytics/time-accuracy')
    assert response.status_code==200
    assert response.json()['total_jobs']==0
    assert response.json()['jobs_with_unknown_estimate']==1



def test_time_accuracy_recent_is_newest_fifty_with_stable_ties(analytics_client,review_db):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    expected=[]
    for index in range(60):
        finished=end-timedelta(minutes=index//2)
        started=finished-timedelta(hours=3)
        identifier=review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,charged_to_org_id,actual_start,actual_end,scheduled_start,scheduled_end) VALUES('Synthetic ordered timing','completed',2,3,1,:start,:end,:start,:scheduled_end) RETURNING id"),{'start':started,'end':finished,'scheduled_end':started+timedelta(hours=2)}).scalar_one()
        expected.append((finished,identifier))
    review_db.commit()
    response=analytics_client.get('/analytics/time-accuracy')
    assert response.status_code==200
    rows=response.json()['recent']
    assert [r['job_id'] for r in rows]==[identifier for _,identifier in sorted(expected,reverse=True)[:50]]
    assert response.json()['total_jobs']==60
    assert all(r['accuracy_pct']==66.7 for r in rows)


def test_time_accuracy_does_not_expose_foreign_related_names(analytics_client,review_db):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(99,'Synthetic private foreign time printer',2,0)"))
    review_db.execute(text("UPDATE models SET name='Synthetic private foreign time model' WHERE id=102"))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,charged_to_org_id,printer_id,model_id,actual_start,actual_end,scheduled_start,scheduled_end) VALUES('Synthetic time job','completed',1,3,1,99,102,:start,:end,:start,:scheduled_end)"),{'start':end-timedelta(hours=3),'end':end,'scheduled_end':end-timedelta(hours=1)})
    review_db.commit()
    response=analytics_client.get('/analytics/time-accuracy')
    assert response.status_code==200
    assert response.json()['total_jobs']==1
    assert response.json()['by_printer']==[] and response.json()['by_model']==[]
    assert 'Synthetic private foreign' not in response.text



def test_time_accuracy_normalizes_mixed_timezone_representations(analytics_client,review_db):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    start=(end-timedelta(hours=3)).astimezone(timezone(timedelta(hours=2)))
    scheduled_start=(end-timedelta(hours=3)).replace(tzinfo=None)
    scheduled_end=(end-timedelta(hours=1)).astimezone(timezone(timedelta(hours=-5)))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,charged_to_org_id,actual_start,actual_end,scheduled_start,scheduled_end) VALUES('Synthetic mixed times','completed',1,3,1,:start,:end,:scheduled_start,:scheduled_end)"),{'start':start.isoformat(),'end':end.isoformat(),'scheduled_start':scheduled_start.isoformat(),'scheduled_end':scheduled_end.isoformat()})
    review_db.commit()
    response=analytics_client.get('/analytics/time-accuracy')
    assert response.status_code==200
    row=response.json()['recent'][0]
    assert row['actual_hours']==3 and row['estimated_hours']==2 and row['accuracy_pct']==66.7



def test_time_accuracy_uses_current_retained_interval_after_completed_job_edit(analytics_client,review_db):
    from datetime import datetime,timedelta,timezone
    end=datetime.now(timezone.utc)-timedelta(minutes=1)
    start=end-timedelta(hours=3)
    identifier=review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,charged_to_org_id,actual_start,actual_end,scheduled_start,scheduled_end) VALUES('Synthetic editable interval','completed',1,3,1,:start,:end,:start,:scheduled_end) RETURNING id"),{'start':start,'end':end,'scheduled_end':start+timedelta(hours=2)}).scalar_one()
    review_db.commit()
    assert analytics_client.get('/analytics/time-accuracy').json()['recent'][0]['estimated_hours']==2
    review_db.execute(text('UPDATE jobs SET scheduled_end=:end WHERE id=:id'),{'end':end,'id':identifier});review_db.commit()
    row=analytics_client.get('/analytics/time-accuracy').json()['recent'][0]
    assert row['estimated_hours']==3 and row['accuracy_pct']==100
    assert analytics_client.get('/analytics/time-accuracy').json()['estimate_basis']=='retained_schedule_interval'
