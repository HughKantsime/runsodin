"""Actual authenticated model ranking zero-value regression."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")


@pytest.mark.parametrize('markup,factor', [(0,0),(None,300),(50,50)])
def test_analytics_model_rankings_preserve_explicit_zero_markup(token_client, review_db, markup, factor):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, _ = token_client
    client.app.include_router(analytics.router)
    identifier = review_db.execute(text("INSERT INTO models(name,org_id,cost_per_item,markup_percent,build_time_hours,units_per_bed) VALUES('Synthetic zero-markup model',1,5,:markup,2,1) RETURNING id"), {'markup':markup}).scalar_one()
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,model_id,charged_to_org_id,created_at) VALUES('Synthetic completed ranking','completed',2,:model,1,CURRENT_TIMESTAMP),('Synthetic projected ranking','pending',2,:model,1,CURRENT_TIMESTAMP)"), {'model':identifier})
    review_db.commit()
    response = client.get('/analytics', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert response.status_code == 200
    body = response.json()
    for field in ['top_by_hour','worst_performers']:
        row = next(row for row in body[field] if row['id']==identifier)
        assert row['value_per_hour']==5*factor/100/2
        assert row['value_per_bed']==5*factor/100
    assert body['summary']['avg_value_per_hour']==5*factor/100/2
    assert body['summary']['total_revenue']==10*factor/100
    assert body['summary']['projected_revenue']==10*factor/100
