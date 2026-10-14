"""Advertised usage-report assertions through actual isolated query service."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_analytics_financial_behavior import analytics_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

@pytest.fixture
def usage_client(analytics_client,review_db,monkeypatch):
    from modules.reporting.routes import analytics
    monkeypatch.setattr(analytics,'require_feature',lambda _:None)
    from datetime import datetime,timedelta,timezone
    from core.economic_identity import _add_created
    from core.base import JobStatus
    from modules.jobs.models import Job
    now=datetime.now(timezone.utc).replace(tzinfo=None)
    _add_created(review_db,Job(item_name='Sanctioned usage',status=JobStatus.COMPLETED,
        quantity=1,duration_hours=2,estimated_cost=5,submitted_by=1,printer_id=9,
        charged_to_org_id=1,actual_start=now-timedelta(hours=3),actual_end=now-timedelta(hours=1),
        scheduled_start=now-timedelta(hours=3),scheduled_end=now-timedelta(hours=1)),
        'job',owner_org_id=1)
    review_db.commit()
    return analytics_client

@pytest.mark.parametrize('days,status',[(6,422),(7,200),(90,200),(91,422)])
def test_usage_time_window_bounds(usage_client,days,status):
    response=usage_client.get('/education/usage-report',params={'days':days})
    assert response.status_code==status
    if status==200: assert response.json()['days']==days


def test_usage_report_exposes_advertised_user_cost_and_accuracy(usage_client):
    response=usage_client.get('/education/usage-report')
    assert response.status_code==200
    user=next(row for row in response.json()['users'] if row['user_id']==1)
    assert user['total_jobs_submitted']>=1 and user['total_print_hours']==2
    assert user['total_cost'] is None and user['currency'] is None
    assert user['cost_basis']=='unbound_estimate_currency'
    assert user['accuracy']==100 and user['accuracy_samples']==1
    # Unknown currency is deliberately not full advertised priced-cost acceptance.


def test_usage_report_aggregates_stats_by_printer_and_model(usage_client):
    response=usage_client.get('/education/usage-report')
    assert response.status_code==200
    assert 'by_printer' in response.json() and 'by_model' in response.json()
