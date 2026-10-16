"""Cert-E (rep): Cost Chargebacks F10-10.7-01/02 through real credentials and real schema.

Authentication is genuine (hashed API tokens / signed JWT resolved against the
bootstrapped SQLite users table). No principal override, no mocked query.
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.test_license_user_limits import signed_license

# Exercises Pro-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/cert-E-rep-chargeback-wire.json'


def _jwt(username):
    from core.auth import create_access_token
    return {'Authorization': 'Bearer ' + create_access_token(data={'sub': username})}


@pytest.fixture
def jobs_client(token_client, review_db, signed_license, monkeypatch):
    from modules.jobs.routes import jobs_crud
    from modules.notifications import alert_dispatcher
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    signed_license(tier='pro', features=[])
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **_: True)
    client, issue = token_client
    client.app.include_router(jobs_crud.router, prefix='/api/v1')
    return client, issue


def _charged(db, job_id):
    with Session(db.get_bind()) as fresh:
        return fresh.execute(text('SELECT charged_to_user_id, charged_to_org_id FROM jobs WHERE id=:id'), {'id': job_id}).one()


@pytest.mark.parametrize('carrier,user_id', [('api', 3), ('jwt-admin', 3), ('jwt-viewer', 1)])
def test_single_job_creation_links_authenticated_user(jobs_client, review_db, carrier, user_id):
    client, issue = jobs_client
    headers = issue(['write:jobs']) if carrier == 'api' else _jwt('admin' if user_id == 3 else 'student')
    response = client.post('/api/v1/jobs', headers=headers, json={'item_name': 'Cert chargeback link'})
    assert response.status_code == 201, response.text
    assert tuple(_charged(review_db, response.json()['id'])) == (user_id, 1)


def test_bulk_batch_and_repeat_creation_link_the_creating_user(jobs_client, review_db):
    client, issue = jobs_client
    headers = issue(['write:jobs'])
    bulk = client.post('/api/v1/jobs/bulk', headers=headers, json=[{'item_name': 'Cert bulk a'}, {'item_name': 'Cert bulk b'}])
    assert bulk.status_code == 201, bulk.text
    batch = client.post('/api/v1/jobs/batch', headers=headers, json={'item_name': 'Cert batch', 'printer_ids': [9, 10]})
    assert batch.status_code in (200, 201), batch.text
    batch_ids = [j['id'] for j in batch.json()]
    assert len(batch_ids) == 2
    # Repeat a job originally charged to someone else: the repeater is charged.
    original = review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,charged_to_user_id,charged_to_org_id,submitted_by) "
                                      "VALUES('Cert original',  'completed',1,1,1,1) RETURNING id")).scalar_one()
    review_db.commit()
    repeat = client.post(f'/api/v1/jobs/{original}/repeat', headers=headers)
    assert repeat.status_code in (200, 201), repeat.text
    repeat_id = repeat.json()['new_job_id']
    for job_id in [j['id'] for j in bulk.json()] + batch_ids + [repeat_id]:
        assert tuple(_charged(review_db, job_id)) == (3, 1), job_id


def _seed_two_users(db):
    db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    rows = [
        # (id, user, cost, hours, created_at, status)
        (7001, 1, 2.0, 1.5, '2026-09-10 09:00:00', 'completed'),
        (7002, 1, 3.0, 2.0, '2026-09-20 09:00:00', 'failed'),
        (7003, 2, 4.0, 3.0, '2026-09-15 09:00:00', 'completed'),
        (7004, 2, 9.0, 9.0, '2026-08-01 09:00:00', 'completed'),   # before window
        (7005, 1, 8.0, 8.0, '2026-10-02 00:00:00', 'completed'),   # after window
        (7006, 2, 6.0, 4.0, '2026-09-30 23:59:59', 'completed'),   # last second of window
    ]
    for identifier, user, cost, hours, created, status in rows:
        db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours,created_at) "
                        "VALUES(:id,'Cert chargeback',:status,1,:user,1,:cost,:hours,:created)"),
                   {'id': identifier, 'status': status, 'user': user, 'cost': cost, 'hours': hours, 'created': created})
    for job, grams, status in [(7001, 10.0, 'completed'), (7002, 5.5, 'failed'), (7003, 20.0, 'completed'), (7006, 4.0, 'completed'), (7004, 999.0, 'completed')]:
        db.execute(text("INSERT INTO print_archives(job_id,print_name,status,filament_used_grams) VALUES(:j,'Cert chargeback',:s,:g)"), {'j': job, 's': status, 'g': grams})
    from core.finance_currency import write_currency
    write_currency(db, owner_scope='org', owner_key=1, currency='EUR')
    db.commit()


def test_chargeback_date_filter_selects_historical_rows_per_user_breakdown(token_client, review_db):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    _seed_two_users(review_db)
    response = client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'start_date': '2026-09-01', 'end_date': '2026-09-30'})
    assert response.status_code == 200, response.text
    rows = {r['user_id']: r for r in response.json()}
    assert set(rows) == {1, 2}
    assert (rows[1]['job_count'], rows[1]['total_hours'], rows[1]['total_grams'], rows[1]['total_cost'], rows[1]['currency']) == (2, 3.5, 15.5, 5.0, 'EUR')
    assert (rows[2]['job_count'], rows[2]['total_hours'], rows[2]['total_grams'], rows[2]['total_cost'], rows[2]['currency']) == (2, 7.0, 24.0, 10.0, 'EUR')
    assert rows[2]['username'] == 'manager'
    # Without filters every consumed job is included (historical rows are not dropped).
    everything = {r['user_id']: r for r in client.get('/reports/chargebacks', headers=issue(['read:analytics'])).json()}
    assert everything[1]['job_count'] == 3 and everything[2]['job_count'] == 3
    # Start-only and end-only filters are honored independently.
    start_only = {r['user_id']: r['job_count'] for r in client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'start_date': '2026-09-16'}).json()}
    assert start_only == {1: 2, 2: 1}
    end_only = {r['user_id']: r['job_count'] for r in client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'end_date': '2026-09-14'}).json()}
    assert end_only == {1: 1, 2: 1}


def test_capture_chargeback_wire_for_system_tab_ui(token_client, review_db):
    """Producer fixture for the SystemTab UI certification test."""
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    _seed_two_users(review_db)
    response = client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'start_date': '2026-09-01', 'end_date': '2026-09-30'})
    assert response.status_code == 200
    fixture = {'request': {'path': '/reports/chargebacks', 'start_date': '2026-09-01', 'end_date': '2026-09-30'}, 'rows': response.json()}
    if FIXTURE.exists():
        assert json.loads(FIXTURE.read_text()) == fixture, 'Producer contract changed; reconcile the consumer explicitly'
    else:
        FIXTURE.write_text(json.dumps(fixture, indent=2) + '\n')
