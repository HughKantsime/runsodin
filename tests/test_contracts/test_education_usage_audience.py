"""Usage report audience boundaries against the real isolated query service."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client


@pytest.mark.parametrize('role',['operator','admin'])
def test_transferred_submitter_does_not_transfer_foreign_job_history(review_db,role):
    from modules.reporting.education_usage import build_education_usage_report
    principal={'id':3,'role':role,'group_id':1}
    review_db.execute(text('UPDATE users SET group_id=1 WHERE id=5'))
    review_db.commit()
    before=build_education_usage_report(30,review_db,principal)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,submitted_by,charged_to_org_id,created_at) VALUES('Retained foreign history','completed',1,17,5,2,CURRENT_TIMESTAMP)"))
    review_db.commit()
    # A legacy row with explicit foreign ownership cannot confer org1 authority
    # merely because its submitter now belongs to org1. This is not a proof of
    # sanctioned economic birth; current-source positives cover that separately.
    after=build_education_usage_report(30,review_db,principal)
    assert {k:v for k,v in after.items() if k!='as_of'}=={k:v for k,v in before.items() if k!='as_of'},'Current membership exposed retained foreign job history'


@pytest.mark.parametrize('role', ['operator', 'admin'])
def test_organization_usage_report_excludes_foreign_users_and_activity(review_db, role):
    from modules.reporting.education_usage import build_education_usage_report
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,submitted_by,charged_to_org_id,created_at) VALUES('Owned usage','completed',1,2,1,1,CURRENT_TIMESTAMP)"))
    review_db.commit()
    principal = {'id':3, 'role':role, 'group_id':1}
    before = build_education_usage_report(30, review_db, principal)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,submitted_by,charged_to_org_id,created_at) VALUES('Foreign usage','completed',1,17,5,2,CURRENT_TIMESTAMP)"))
    review_db.commit()
    report = build_education_usage_report(30, review_db, principal)
    assert {k:v for k,v in report.items() if k!='as_of'} == {k:v for k,v in before.items() if k!='as_of'}



def test_platform_admin_usage_report_preserves_cross_organization_view(review_db):
    from modules.reporting.education_usage import build_education_usage_report
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,submitted_by,charged_to_org_id,created_at) VALUES('Owned usage','completed',1,1,1,CURRENT_TIMESTAMP),('Foreign usage','completed',1,5,2,CURRENT_TIMESTAMP)"))
    review_db.commit()
    report = build_education_usage_report(30, review_db, {'id':3, 'role':'admin', 'group_id':None})
    assert {row['user_id'] for row in report['users']} == {1,5}


@pytest.mark.parametrize('group', [None, 0, False, -1, '1', 1.0])
def test_unassigned_or_invalid_nonplatform_usage_audience_is_empty(review_db, group):
    from modules.reporting.education_usage import build_education_usage_report
    report = build_education_usage_report(30, review_db, {'id':3, 'role':'operator', 'group_id':group})
    assert report['users'] == []
    assert report['summary']['total_jobs'] == 0
    assert report['daily_submissions'] == {}


@pytest.mark.parametrize('carrier', ['jwt', 'api'])
@pytest.mark.parametrize('platform', [False, True])
def test_actual_authenticated_usage_audience(token_client, review_db, monkeypatch, carrier, platform):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    monkeypatch.setattr(analytics, 'require_feature', lambda _:None)
    if platform:
        review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,duration_hours,submitted_by,charged_to_org_id,created_at) VALUES('Foreign usage','completed',1,17,5,2,CURRENT_TIMESTAMP)"))
    review_db.commit()
    headers = issue(['read:analytics']) if carrier == 'api' else {'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})}
    response = client.get('/education/usage-report', headers=headers)
    assert response.status_code == 200
    identifiers = {row['user_id'] for row in response.json()['users']}
    assert (5 in identifiers) is platform


def test_actual_scoped_token_cannot_read_usage_with_other_resource_scope(token_client, monkeypatch):
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    monkeypatch.setattr(analytics, 'require_feature', lambda _:None)
    headers = issue(['read:jobs'])
    response = client.get('/education/usage-report', headers=headers)
    assert response.status_code == 403
