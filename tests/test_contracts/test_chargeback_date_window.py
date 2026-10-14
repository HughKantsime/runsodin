"""Chargeback calendar-day boundaries through genuine owned API credentials."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


def test_chargeback_end_day_includes_midnight_and_final_second(token_client, review_db):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,created_at) VALUES('Synthetic midnight',1,1,'2026-10-12 00:00:00'),('Synthetic final second',1,1,'2026-10-12 23:59:59.999999'),('Synthetic following midnight',1,1,'2026-10-13 00:00:00')"))
    review_db.commit()
    result = client.get('/reports/chargebacks', headers=issue(['read:analytics']),
                        params={'start_date':'2026-10-12','end_date':'2026-10-12'})
    assert result.status_code == 200
    assert len(result.json()) == 1, 'Selected end day was excluded'
    assert result.json()[0]['job_count'] == 2


@pytest.mark.parametrize('parameters', [
    {'start_date':'2026-02-30'}, {'end_date':'garbage'},
    {'start_date':'2026-10-13','end_date':'2026-10-12'},
])
def test_chargeback_invalid_calendar_window_refuses(token_client, parameters):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    result = client.get('/reports/chargebacks', headers=issue(['read:analytics']), params=parameters)
    assert result.status_code == 422


@pytest.mark.parametrize('field', ['start_date', 'end_date'])
@pytest.mark.parametrize('value', ['20261012', '2026-10-12T00:00:00Z', '２０２６-10-12', '2026-1-2'])
def test_chargeback_requires_exact_ascii_date(token_client, field, value):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    assert client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={field:value}).status_code == 422


def test_chargeback_maximum_day_keeps_finite_upper_bound(token_client, review_db):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,created_at) VALUES('Synthetic finite max',1,1,'9999-12-31 23:59:59.999999')"))
    review_db.commit()
    result = client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'start_date':'9999-12-31','end_date':'9999-12-31'})
    assert result.status_code == 200
    assert result.json()[0]['job_count'] == 1
