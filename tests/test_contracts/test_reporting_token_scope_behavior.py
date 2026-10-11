"""Real narrow credentials cannot administer global report schedules."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def reporting_token_client(token_client,review_db):
    from modules.reporting.routes import reports,analytics
    client,issue=token_client;client.app.include_router(reports.router);client.app.include_router(analytics.router)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO report_schedules(id,name,report_type,frequency,recipients,is_active) VALUES(199,'Synthetic protected report','job_summary','daily','[\"synthetic@example.test\"]',0)"));review_db.commit()
    return client,issue

@pytest.mark.parametrize('scope',['read','write:jobs','write:printers'])
@pytest.mark.parametrize('operation',['list','create','update','delete'])
def test_narrow_token_cannot_administer_global_reports(reporting_token_client,review_db,scope,operation):
    client,issue=reporting_token_client;headers=issue([scope])
    before=tuple(review_db.execute(text('SELECT id,name,is_active FROM report_schedules')).all())
    if operation=='list':response=client.get('/report-schedules',headers=headers)
    elif operation=='create':response=client.post('/report-schedules',headers=headers,json={'name':'Denied','report_type':'job_summary','recipients':['synthetic@example.test'],'is_active':False})
    elif operation=='update':response=client.patch('/report-schedules/199',headers=headers,json={'is_active':True})
    else:response=client.delete('/report-schedules/199',headers=headers)
    assert response.status_code==403
    review_db.expire_all()
    assert tuple(review_db.execute(text('SELECT id,name,is_active FROM report_schedules')).all())==before

@pytest.mark.parametrize('path',['/stats','/analytics','/analytics/failures','/analytics/time-accuracy','/reports/chargebacks'])
def test_unrelated_scope_cannot_read_reporting_data(reporting_token_client,path):
    client,issue=reporting_token_client
    assert client.get(path,headers=issue(['write:jobs'])).status_code==403


@pytest.mark.parametrize('scope',['read:analytics','read','admin'])
@pytest.mark.parametrize('path',['/stats','/analytics','/analytics/failures','/analytics/time-accuracy','/reports/chargebacks'])
def test_reporting_read_grants_remain_usable(reporting_token_client,path,scope):
    client,issue=reporting_token_client
    assert client.get(path,headers=issue([scope])).status_code==200


def test_admin_token_can_manage_global_report_records(reporting_token_client):
    client,issue=reporting_token_client;headers=issue(['admin'])
    assert client.get('/report-schedules',headers=headers).status_code==200
    assert client.patch('/report-schedules/199',headers=headers,json={'is_active':True}).status_code==200
    assert client.delete('/report-schedules/199',headers=headers).status_code==200

@pytest.mark.parametrize('scope',['read','write:jobs','read:analytics'])
def test_narrow_token_cannot_trigger_run_now_sender(reporting_token_client,scope,monkeypatch):
    from modules.reporting import report_runner
    client,issue=reporting_token_client;calls=[]
    monkeypatch.setattr(report_runner,'run_report',lambda schedule:calls.append(schedule['id']))
    response=client.post('/report-schedules/199/run',headers=issue([scope]))
    assert response.status_code==403 and calls==[]


def test_unrelated_token_is_denied_before_education_report_license_or_query(reporting_token_client,monkeypatch):
    from modules.reporting.routes import analytics
    client,issue=reporting_token_client;calls=[]
    monkeypatch.setattr(analytics,'require_feature',lambda feature:calls.append(feature))
    response=client.get('/education/usage-report',headers=issue(['write:jobs']))
    assert response.status_code==403 and calls==[]
