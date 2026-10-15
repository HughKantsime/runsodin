"""Real credential chargeback privacy regression in an owned database."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")


@pytest.mark.parametrize('carrier', ['jwt', 'api'])
def test_org_chargeback_does_not_adopt_unassigned_foreign_history(token_client, review_db, carrier):
    from core.auth import create_access_token
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    headers = (issue(['read:analytics']) if carrier == 'api' else
               {'Authorization': 'Bearer ' + create_access_token(data={'sub': 'admin'})})
    before = client.get('/reports/chargebacks', headers=headers)
    assert before.status_code == 200
    review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours) VALUES('Synthetic foreign unassigned history',5,NULL,123,17)"))
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE item_name LIKE 'Synthetic%'"))  # chargebacks cover consumed (terminal) jobs
    review_db.commit()
    after = client.get('/reports/chargebacks', headers=headers)
    assert after.status_code == 200
    assert after.json() == before.json(), 'Unassigned foreign history disclosed to organization admin'


@pytest.mark.parametrize('platform', [False, True])
@pytest.mark.parametrize('carrier', ['jwt', 'api'])
def test_chargeback_preserves_exact_org_and_platform_views(token_client, review_db, platform, carrier):
    from core.auth import create_access_token
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    if platform:
        review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours) VALUES('Synthetic exact org',1,1,5,2),('Synthetic foreign unassigned',5,NULL,7,3),('Synthetic explicit foreign',5,2,11,4)"))
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE item_name LIKE 'Synthetic%'"))  # chargebacks cover consumed (terminal) jobs
    review_db.commit()
    headers = (issue(['read:analytics']) if carrier == 'api' else
               {'Authorization': 'Bearer ' + create_access_token(data={'sub': 'admin'})})
    response = client.get('/reports/chargebacks', headers=headers)
    assert response.status_code == 200
    rows = {r['user_id']: r for r in response.json()}
    assert rows[1]['job_count'] >= 1
    assert (5 in rows) is platform
    if platform:
        assert rows[5]['job_count'] == 2


@pytest.mark.parametrize('group', [0, False, -1, '1', 1.0])
def test_invalid_chargeback_scope_cannot_become_platform(review_db, group):
    import asyncio
    from modules.reporting.routes.reports import chargeback_report
    result = asyncio.run(chargeback_report(current_user={'id':3,'role':'admin','group_id':group},db=review_db))
    assert result == []
