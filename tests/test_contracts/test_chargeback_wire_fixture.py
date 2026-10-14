"""Capture the actual owned HTTP producer shape for the UI contract test."""
import json
from pathlib import Path
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


def test_capture_chargeback_wire_from_real_scoped_http(token_client, review_db):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    for _ in range(5):
        review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours,created_at) VALUES('Synthetic wire sample',1,1,1.45,0.5,'2026-10-12 12:00:00')"))
    review_db.commit()
    response = client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'start_date':'2026-10-12','end_date':'2026-10-12'})
    assert response.status_code == 200
    assert response.json() == [{'user_id':1,'username':'student','job_count':5,'total_cost':7.25,'total_hours':2.5,'total_grams':0.0,'unmeasured_prints':0,'currency':None,'cost_basis':'job_estimate_current_currency'}]
    fixture = {'scope':'Owned genuine scoped HTTP response; unitless legacy money is not currency-qualified; grams are measured print-attempt filament (none printed here)', 'rows':response.json()}
    path = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/chargeback-wire-owned.json'
    if path.exists():
        assert json.loads(path.read_text()) == fixture, 'Producer contract changed; reconcile the consumer explicitly'
    else:
        path.write_text(json.dumps(fixture,indent=2)+'\n')
