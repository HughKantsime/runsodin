"""Cert-E (rep): Usage Reports F10-10.8-01..04.

Real hashed API-token / JWT authentication, real Ed25519-signed ephemeral
license (no require_feature substitution), real bootstrapped SQLite schema and
known-value jobs whose created_at spans the advertised 7-90 day window.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.test_license_user_limits import signed_license

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/cert-E-rep-usage-wire.json'
VOLATILE = ('as_of', 'last_activity')


@pytest.fixture
def usage_client(token_client, review_db, signed_license):
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    signed_license(tier='pro', features=[])
    return client, issue


def _job(db, *, user, printer, model, status, cost, age_days, actual=None, scheduled=None):
    from core.economic_identity import _add_created
    from core.base import JobStatus
    from modules.jobs.models import Job
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    created = now - timedelta(days=age_days, minutes=5)
    end = created + timedelta(hours=6)
    fields = dict(item_name='Cert usage', status=JobStatus(status), quantity=1, duration_hours=actual or 1,
                  estimated_cost=cost, submitted_by=user, printer_id=printer, model_id=model, charged_to_org_id=1,
                  created_at=created)
    if actual is not None:
        fields.update(actual_start=end - timedelta(hours=actual), actual_end=end)
    if scheduled is not None:
        fields.update(scheduled_start=end - timedelta(hours=scheduled), scheduled_end=end)
    _add_created(db, Job(**fields), 'job', owner_org_id=1)


@pytest.fixture
def seeded(review_db):
    from core.finance_currency import write_currency
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(201,'Cert bracket',1),(202,'Cert hinge',1)"))
    _job(review_db, user=1, printer=9, model=201, status='completed', cost=5, age_days=1, actual=2, scheduled=2)    # A
    _job(review_db, user=1, printer=10, model=201, status='completed', cost=3, age_days=10, actual=3, scheduled=2)  # B
    _job(review_db, user=2, printer=9, model=202, status='failed', cost=4, age_days=2)                             # C
    _job(review_db, user=2, printer=9, model=202, status='completed', cost=1, age_days=60, actual=1, scheduled=1)  # D
    _job(review_db, user=1, printer=9, model=201, status='completed', cost=10, age_days=89, actual=4, scheduled=4) # E
    _job(review_db, user=1, printer=9, model=201, status='completed', cost=50, age_days=95, actual=9, scheduled=9) # outside any window
    write_currency(review_db, owner_scope='org', owner_key=1, currency='EUR')
    review_db.commit()


def _metrics(row):
    return (row['total_jobs_submitted'], row['total_print_hours'], row['total_cost'], row['accuracy'])


def _report(client, issue, days=None):
    params = {} if days is None else {'days': days}
    response = client.get('/education/usage-report', headers=issue(['read:analytics']), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_per_user_job_count_hours_cost_accuracy_known_values(usage_client, seeded):
    client, issue = usage_client
    body = _report(client, issue, 90)
    users = {u['user_id']: u for u in body['users']}
    assert set(users) == {1, 2}
    assert _metrics(users[1]) == (3, 9.0, 18.0, 88.9)
    assert _metrics(users[2]) == (2, 1.0, 5.0, 100.0)
    assert users[1]['currency'] == 'EUR' and users[1]['accuracy_samples'] == 3
    assert users[2]['total_jobs_failed'] == 1 and users[2]['accuracy_samples'] == 1


@pytest.mark.parametrize('days,expected', [
    (7, {1: (1, 2.0, 5.0, 100.0), 2: (1, 0.0, 4.0, None)}),
    (14, {1: (2, 5.0, 8.0, 83.3), 2: (1, 0.0, 4.0, None)}),
    (30, {1: (2, 5.0, 8.0, 83.3), 2: (1, 0.0, 4.0, None)}),
    (61, {1: (2, 5.0, 8.0, 83.3), 2: (2, 1.0, 5.0, 100.0)}),
    (90, {1: (3, 9.0, 18.0, 88.9), 2: (2, 1.0, 5.0, 100.0)}),
])
def test_window_selects_historical_rows_by_creation_age(usage_client, seeded, days, expected):
    client, issue = usage_client
    body = _report(client, issue, days)
    assert body['days'] == days
    assert {u['user_id']: _metrics(u) for u in body['users']} == expected


def test_default_window_is_thirty_days(usage_client, seeded):
    client, issue = usage_client
    body = _report(client, issue)
    assert body['days'] == 30
    assert {u['user_id']: u['total_jobs_submitted'] for u in body['users']} == {1: 2, 2: 1}


@pytest.mark.parametrize('days', [0, 6, 91, 365, 'thirty'])
def test_window_outside_seven_to_ninety_is_rejected(usage_client, seeded, days):
    client, issue = usage_client
    response = client.get('/education/usage-report', headers=issue(['read:analytics']), params={'days': days})
    assert response.status_code == 422


def test_aggregates_by_printer_and_by_model_known_values(usage_client, seeded):
    client, issue = usage_client
    body = _report(client, issue, 90)
    printers = {p['id']: p for p in body['by_printer']}
    models = {m['id']: m for m in body['by_model']}
    assert set(printers) == {9, 10} and set(models) == {201, 202}
    assert printers[9]['name'] == 'P1S-09' and printers[10]['name'] == 'P1S-10'
    assert _metrics(printers[9]) == (4, 7.0, 20.0, 100.0)
    assert _metrics(printers[10]) == (1, 3.0, 3.0, 66.7)
    assert models[201]['name'] == 'Cert bracket' and models[202]['name'] == 'Cert hinge'
    assert _metrics(models[201]) == (3, 9.0, 18.0, 88.9)
    assert _metrics(models[202]) == (2, 1.0, 5.0, 100.0)
    # Window also bounds the aggregates.
    week = _report(client, issue, 7)
    assert {p['id']: p['total_jobs_submitted'] for p in week['by_printer']} == {9: 2}
    assert {m['id']: m['total_jobs_submitted'] for m in week['by_model']} == {201: 1, 202: 1}


@pytest.mark.parametrize('tier,status', [('community', 403), ('pro', 200), ('education', 200), ('enterprise', 200)])
@pytest.mark.parametrize('carrier', ['api', 'jwt'])
def test_usage_report_is_gated_by_signed_license_tier(token_client, review_db, signed_license, seeded, tier, status, carrier):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, issue = token_client
    client.app.include_router(analytics.router)
    signed_license(tier=tier, features=[])
    headers = issue(['read:analytics']) if carrier == 'api' else {'Authorization': 'Bearer ' + create_access_token(data={'sub': 'admin'})}
    response = client.get('/education/usage-report', headers=headers, params={'days': 30})
    assert response.status_code == status, response.text
    if status == 403:
        assert 'usage_reports' in response.json()['detail'] and 'Pro' in response.json()['detail']
        assert 'users' not in response.text


def test_pro_tier_definition_and_unsigned_license_cannot_unlock(review_db, signed_license, tmp_path, monkeypatch):
    import license_manager as lm
    assert 'usage_reports' in lm.TIERS['pro']['features']
    assert 'usage_reports' not in lm.TIERS['community']['features']
    # A genuinely signed Pro license unlocks the feature via tier defaults; a
    # payload altered after signing (tier upgrade) fails verification.
    _, info = signed_license(tier='pro', features=[])
    assert info.valid and info.has_feature('usage_reports')
    path = Path(lm._find_license_file())
    raw, signature = path.read_text().split('.')
    import base64
    tampered = base64.urlsafe_b64encode(json.dumps({'tier': 'enterprise', 'licensee': 'Tampered', 'expires_at': '2099-01-01', 'features': []}).encode()).decode()
    path.write_text(tampered + '.' + signature)
    forged = lm.load_license()
    assert not forged.has_feature('usage_reports')


def _normalized(body):
    body = json.loads(json.dumps(body))
    body.pop('as_of', None)
    body.pop('daily_submissions', None)
    for user in body.get('users', []):
        user.pop('last_activity', None)
    return body


def test_capture_usage_wire_for_reports_ui(usage_client, seeded):
    client, issue = usage_client
    windows = {str(days): _report(client, issue, days) for days in (7, 30, 90)}
    fixture = {'request': {'path': '/education/usage-report', 'days': [7, 30, 90]}, 'windows': windows}
    if FIXTURE.exists():
        stored = json.loads(FIXTURE.read_text())
        assert stored['request'] == fixture['request']
        assert {k: _normalized(v) for k, v in stored['windows'].items()} == {k: _normalized(v) for k, v in windows.items()}, \
            'Producer contract changed; reconcile the consumer explicitly'
    else:
        FIXTURE.write_text(json.dumps(fixture, indent=2) + '\n')


LICENSE_FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/cert-E-rep-license-wire.json'


def test_capture_public_license_wire_for_pro_gate_ui(signed_license, monkeypatch):
    """Real signed Community and Pro licenses through the real public /license route."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.system import routes_health
    monkeypatch.delenv('ODIN_LICENSE_FILE', raising=False)
    bodies = {}
    for tier in ('community', 'pro'):
        _, info = signed_license(tier=tier, features=[], expires_at='2099-01-01')
        monkeypatch.setattr(routes_health, 'get_license', lambda info=info: info)
        app = FastAPI()
        app.add_api_route('/license', routes_health.get_license_info)
        response = TestClient(app).get('/license')
        assert response.status_code == 200
        bodies[tier] = {k: v for k, v in response.json().items() if k != 'license_sha256'}
    assert 'usage_reports' in bodies['pro']['features'] and 'usage_reports' not in bodies['community']['features']
    fixture = {'scope': 'Real Ed25519-signed ephemeral licenses (features=[] -> tier defaults) via public /license; digest omitted', 'bodies': bodies}
    if LICENSE_FIXTURE.exists():
        assert json.loads(LICENSE_FIXTURE.read_text()) == fixture, 'Producer contract changed; reconcile the consumer explicitly'
    else:
        LICENSE_FIXTURE.write_text(json.dumps(fixture, indent=2) + '\n')
