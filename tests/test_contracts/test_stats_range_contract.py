"""Actual HTTP range regression; synthetic isolated records only."""
import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from tests.test_contracts.test_analytics_financial_behavior import analytics_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client


def test_selected_stats_range_excludes_older_completed_job(analytics_client, review_db):
    finished = datetime.now(timezone.utc) - timedelta(days=10)
    from core.economic_identity import _add_created
    from core.base import JobStatus
    from modules.jobs.models import Job
    _add_created(review_db, Job(item_name='Synthetic owned old utilization', status=JobStatus.COMPLETED, quantity=1, printer_id=9, submitted_by=1, charged_to_org_id=1, actual_start=(finished-timedelta(hours=2)).replace(tzinfo=None), actual_end=finished.replace(tzinfo=None)), 'job', owner_org_id=1)
    review_db.commit()
    short = analytics_client.get('/stats?days=7')
    long = analytics_client.get('/stats?days=30')
    assert short.status_code == long.status_code == 200
    short_row = next(row for row in short.json()['printer_stats'] if row['id'] == 9)
    long_row = next(row for row in long.json()['printer_stats'] if row['id'] == 9)
    assert long_row['completed_jobs'] == short_row['completed_jobs'] + 1


def test_printer_stats_exclude_foreign_jobs_on_shared_printer(analytics_client, review_db):
    before = analytics_client.get('/stats')
    assert before.status_code == 200
    original = next(row for row in before.json()['printer_stats'] if row['id'] == 9)
    finished = datetime.now(timezone.utc) - timedelta(hours=1)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,charged_to_org_id,actual_start,actual_end) VALUES('Synthetic foreign utilization','completed',1,9,2,:start,:end)"), {'start': finished-timedelta(hours=2), 'end': finished})
    review_db.commit()
    after = analytics_client.get('/stats')
    assert after.status_code == 200
    current = next(row for row in after.json()['printer_stats'] if row['id'] == 9)
    assert current['completed_jobs'] == original['completed_jobs']


@pytest.mark.parametrize('carrier', ['jwt', 'api'])
def test_authenticated_stats_exclude_foreign_printer_job(token_client, review_db, carrier):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    headers = issue(['read:analytics']) if carrier == 'api' else {'Authorization': 'Bearer ' + create_access_token(data={'sub': 'admin'})}
    before = client.get('/stats', headers=headers)
    assert before.status_code == 200
    original = next(row for row in before.json()['printer_stats'] if row['id'] == 9)
    finished = datetime.now(timezone.utc) - timedelta(hours=1)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,charged_to_org_id,actual_start,actual_end) VALUES('Synthetic authenticated foreign','completed',1,9,2,:start,:end)"), {'start': finished-timedelta(hours=2), 'end': finished})
    review_db.commit()
    after = client.get('/stats', headers=headers)
    assert after.status_code == 200
    current = next(row for row in after.json()['printer_stats'] if row['id'] == 9)
    assert current['completed_jobs'] == original['completed_jobs']


@pytest.mark.parametrize('days', ['0', '-1', '366', 'not-a-number'])
def test_stats_reject_invalid_range(analytics_client, days):
    assert analytics_client.get('/stats', params={'days': days}).status_code == 422


@pytest.mark.parametrize('carrier', ['jwt', 'api'])
def test_authenticated_analytics_exclude_foreign_failure_on_visible_printer(token_client, review_db, carrier):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    headers = issue(['read:analytics']) if carrier == 'api' else {'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})}
    before = client.get('/analytics', headers=headers)
    assert before.status_code == 200
    original = next(row for row in before.json()['printer_stats'] if row['id']==9)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,charged_to_org_id,created_at) VALUES('Synthetic foreign failure','failed',1,9,2,CURRENT_TIMESTAMP)"))
    review_db.commit()
    after = client.get('/analytics', headers=headers)
    assert after.status_code == 200
    current = next(row for row in after.json()['printer_stats'] if row['id']==9)
    assert current['failed_jobs']==original['failed_jobs']
    assert current['success_rate']==original['success_rate']


@pytest.mark.parametrize('platform', [False, True])
@pytest.mark.parametrize('carrier', ['jwt', 'api'])
def test_analytics_keeps_own_and_explicit_platform_failure_views(token_client, review_db, platform, carrier):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    if platform:
        review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,charged_to_org_id,created_at) VALUES('Synthetic own failure','failed',1,9,1,CURRENT_TIMESTAMP),('Synthetic foreign failure','failed',1,9,2,CURRENT_TIMESTAMP)"))
    review_db.commit()
    headers = issue(['read:analytics']) if carrier == 'api' else {'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})}
    response = client.get('/analytics', headers=headers)
    assert response.status_code == 200
    row = next(row for row in response.json()['printer_stats'] if row['id']==9)
    assert row['failed_jobs']==(2 if platform else 1)
    assert row['success_rate']==0


def test_selected_stats_range_applies_to_mqtt_jobs_in_mixed_timestamp_formats(analytics_client, review_db):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).replace(tzinfo=None).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    for status, ended in [('completed', old), ('completed', recent), ('failed', old)]:
        review_db.execute(text("INSERT INTO print_jobs(printer_id,job_id,filename,status,started_at,ended_at) VALUES(9,:job,'synthetic.3mf',:status,:ended,:ended)"),
                          {'job': f'synthetic-{status}-{ended}', 'status': status, 'ended': ended})
    review_db.commit()
    rows = {days: next(row for row in analytics_client.get(f'/stats?days={days}').json()['printer_stats'] if row['id'] == 9) for days in (7, 30)}
    assert rows[30]['completed_jobs'] == rows[7]['completed_jobs'] + 1
    assert rows[30]['failed_jobs'] == rows[7]['failed_jobs'] + 1
