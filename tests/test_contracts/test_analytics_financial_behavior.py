"""Known-value HTTP checks for legacy estimated production analytics."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")


@pytest.fixture
def analytics_client(bom_client):
    from modules.reporting.routes import analytics
    bom_client.app.include_router(analytics.router)
    return bom_client


@pytest.mark.parametrize('status', ['completed', 'pending'])
def test_explicit_zero_price_does_not_fall_back_to_catalog(analytics_client, review_db, status):
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,suggested_price,estimated_cost,model_id,charged_to_org_id) VALUES('Synthetic free production',:status,2,0,3,101,1)"), {'status':status})
    review_db.commit()
    response=analytics_client.get('/analytics')
    assert response.status_code==200
    summary=response.json()['summary']
    prefix='total' if status=='completed' else 'projected'
    assert summary[f'{prefix}_revenue']==0
    assert summary[f'{prefix}_cost']==6
    if status=='completed':
        assert summary['total_margin']==-6


def test_zero_cost_preserves_estimated_margin(analytics_client, review_db):
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,suggested_price,estimated_cost,charged_to_org_id) VALUES('Synthetic zero cost','completed',2,5,0,1)"))
    review_db.commit()
    summary=analytics_client.get('/analytics').json()['summary']
    assert summary['total_revenue']==10
    assert summary['total_cost']==0
    assert summary['total_margin']==10
    assert summary['margin_percent']==100


@pytest.mark.parametrize('status', ['completed','pending'])
def test_unknown_legacy_quantity_does_not_crash_or_invent_value(analytics_client, review_db, status):
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,suggested_price,estimated_cost,charged_to_org_id) VALUES('Synthetic unknown quantity',:status,NULL,5,3,1)"), {'status':status})
    review_db.commit()
    response=analytics_client.get('/analytics')
    assert response.status_code==200
    summary=response.json()['summary']
    prefix='total' if status=='completed' else 'projected'
    assert summary[f'{prefix}_revenue']==0
    assert summary[f'{prefix}_cost']==0
    assert summary['jobs_with_unknown_quantity']==1


@pytest.mark.parametrize('quantity', [None,0,-2])
def test_invalid_quantity_preserves_completed_duration_only(analytics_client, review_db, quantity):
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,suggested_price,estimated_cost,duration_hours,charged_to_org_id) VALUES('Synthetic invalid completed','completed',:quantity,5,3,2.5,1),('Synthetic invalid pending','pending',:quantity,5,3,4,1)"), {'quantity':quantity})
    review_db.commit()
    summary=analytics_client.get('/analytics').json()['summary']
    assert summary['jobs_with_unknown_quantity']==2
    assert summary['total_print_hours']==2.5
    assert all(summary[key]==0 for key in ('total_revenue','total_cost','projected_revenue','projected_cost'))


def test_tenant_filter_applies_to_values_and_unknown_counter(analytics_client, review_db):
    before=analytics_client.get('/analytics').json()['summary']
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,suggested_price,estimated_cost,charged_to_org_id) VALUES('Synthetic foreign known','completed',2,1000,500,2),('Synthetic foreign unknown','pending',NULL,1000,500,2),('Synthetic shared known','completed',2,5,3,NULL),('Synthetic shared unknown','pending',NULL,5,3,NULL)"))
    review_db.commit()
    summary=analytics_client.get('/analytics').json()['summary']
    assert summary['total_jobs']==before['total_jobs']+2
    assert summary['total_revenue']==before['total_revenue']+10
    assert summary['total_cost']==before['total_cost']+6
    assert summary['jobs_with_unknown_quantity']==before['jobs_with_unknown_quantity']+1
