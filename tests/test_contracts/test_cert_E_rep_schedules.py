"""Cert-E (rep): Scheduled Reports F10-10.9-01..06.

Real hashed admin API token (superadmin: admin with no org) against the real
reports router and bootstrapped SQLite schema. Delivery is proven at the SMTP
wire: report_runner's real smtplib client talks to a local SMTP peer on
127.0.0.1 (ephemeral port), configured through the real system_config row.
"""
import email
import json
from email import policy
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.test_cert_E_rep_smtp_peer import SmtpPeer

# Exercises Pro-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/cert-E-rep-schedules-wire.json'
TYPES = {
    'fleet_utilization': 'Fleet Utilization Report',
    'job_summary': 'Job Summary Report',
    'filament_consumption': 'Filament Consumption Report',
    'failure_analysis': 'Failure Analysis Report',
    'chargeback_summary': 'Chargeback Summary Report',
}


@pytest.fixture
def admin_client(token_client, review_db, monkeypatch):
    from modules.reporting.routes import reports
    from modules.reporting import report_runner
    client, issue = token_client
    client.app.include_router(reports.router)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))  # superadmin
    review_db.commit()
    # The runner opens its own session; point it at the same real test database.
    monkeypatch.setattr(report_runner, 'SessionLocal', sessionmaker(bind=review_db.get_bind()))
    return client, issue(['admin'])


def _configure_smtp(db, peer, **extra):
    db.execute(text("INSERT OR REPLACE INTO system_config(key,value) VALUES('smtp_config',:v)"), {'v': json.dumps(peer.config(**extra))})
    db.commit()


def _html(message):
    parsed = email.message_from_bytes(message['data'], policy=policy.default)
    parts = [p for p in parsed.walk() if p.get_content_type() == 'text/html']
    assert len(parts) == 1, 'Report email must carry exactly one HTML body part'
    return parsed, parts[0].get_content()


def _create(client, headers, **overrides):
    body = {'name': 'Cert weekly', 'report_type': 'job_summary', 'frequency': 'weekly', 'recipients': ['ops@example.test'], **overrides}
    response = client.post('/report-schedules', headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()['id']


def _row(client, headers, identifier):
    return next(r for r in client.get('/report-schedules', headers=headers).json() if r['id'] == identifier)


# ---------------- F10-10.9-01 CRUD ----------------

@pytest.mark.parametrize('frequency', ['daily', 'weekly', 'monthly'])
def test_crud_name_type_frequency_recipients(admin_client, frequency):
    client, headers = admin_client
    identifier = _create(client, headers, name='Cert CRUD', report_type='failure_analysis', frequency=frequency, recipients=['a@example.test', 'b@example.test'])
    row = _row(client, headers, identifier)
    assert (row['name'], row['report_type'], row['frequency'], row['recipients'], row['is_active']) == ('Cert CRUD', 'failure_analysis', frequency, ['a@example.test', 'b@example.test'], True)
    assert row['next_run_at'] is not None and row['last_run_at'] is None
    assert client.patch(f'/report-schedules/{identifier}', headers=headers, json={'name': 'Cert renamed', 'frequency': 'daily', 'recipients': ['c@example.test']}).status_code == 200
    row = _row(client, headers, identifier)
    assert (row['name'], row['frequency'], row['recipients']) == ('Cert renamed', 'daily', ['c@example.test'])
    assert client.delete(f'/report-schedules/{identifier}', headers=headers).status_code == 200
    assert all(r['id'] != identifier for r in client.get('/report-schedules', headers=headers).json())
    assert client.delete(f'/report-schedules/{identifier}', headers=headers).status_code == 404


@pytest.mark.parametrize('body', [
    {'report_type': 'revenue_forecast'}, {'frequency': 'hourly'}, {'name': '  '}, {'recipients': []}, {'recipients': ['not-an-address']},
])
def test_create_rejects_invalid_fields_without_writing(admin_client, review_db, body):
    client, headers = admin_client
    before = review_db.execute(text('SELECT COUNT(*) FROM report_schedules')).scalar_one()
    payload = {'name': 'Cert invalid', 'report_type': 'job_summary', 'frequency': 'weekly', 'recipients': ['ops@example.test'], **body}
    assert client.post('/report-schedules', headers=headers, json=payload).status_code == 400
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM report_schedules')).scalar_one() == before


def test_patch_updates_report_type(admin_client):
    client, headers = admin_client
    identifier = _create(client, headers, report_type='job_summary')
    response = client.patch(f'/report-schedules/{identifier}', headers=headers, json={'report_type': 'chargeback_summary'})
    assert response.status_code == 200
    assert _row(client, headers, identifier)['report_type'] == 'chargeback_summary'
    # An invalid type is refused (never a silent 200) and the stored type is unchanged.
    refused = client.patch(f'/report-schedules/{identifier}', headers=headers, json={'report_type': 'revenue_forecast'})
    assert refused.status_code == 400
    assert _row(client, headers, identifier)['report_type'] == 'chargeback_summary'


# ---------------- F10-10.9-02 five supported types ----------------

def test_exactly_the_five_advertised_types_are_accepted(admin_client):
    from modules.reporting.routes.reports import REPORT_TYPES
    from modules.reporting.report_runner import GENERATORS
    assert REPORT_TYPES == list(TYPES) and set(GENERATORS) == set(TYPES)
    client, headers = admin_client
    for report_type in TYPES:
        identifier = _create(client, headers, name=f'Cert {report_type}', report_type=report_type)
        assert _row(client, headers, identifier)['report_type'] == report_type


@pytest.mark.parametrize('report_type', list(TYPES))
def test_each_type_generates_and_delivers_html_over_smtp(admin_client, review_db, report_type):
    client, headers = admin_client
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,estimated_cost,duration_hours,charged_to_user_id,fail_reason,created_at) "
                           "VALUES('Cert typed','completed',1,5,2,1,NULL,CURRENT_TIMESTAMP),('Cert typed','failed',1,1,1,1,'Cert nozzle clog',CURRENT_TIMESTAMP)"))
    review_db.commit()
    identifier = _create(client, headers, name=f'Cert {report_type}', report_type=report_type)
    with SmtpPeer() as peer:
        _configure_smtp(review_db, peer)
        response = client.post(f'/report-schedules/{identifier}/run', headers=headers)
        assert response.status_code == 200, response.text
        message = peer.wait_for(1)[0]
    parsed, html = _html(message)
    assert parsed['Subject'] == f'O.D.I.N. Report: Cert {report_type}'
    assert TYPES[report_type] in html
    if report_type == 'chargeback_summary':
        assert 'student' in html and '>2<' in html
    if report_type == 'failure_analysis':
        assert 'Cert nozzle clog' in html


def test_scheduled_chargeback_summary_does_not_presume_usd(review_db):
    from core.finance_currency import write_currency
    from modules.reporting.report_runner import generate_chargeback_summary
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,estimated_cost,duration_hours,charged_to_user_id,charged_to_org_id,created_at) "
                           "VALUES('Cert priced','completed',1,4.5,1,1,1,CURRENT_TIMESTAMP),('Cert unpriced','completed',1,NULL,1,1,1,CURRENT_TIMESTAMP)"))
    write_currency(review_db, owner_scope='org', owner_key=1, currency='EUR')
    review_db.commit()
    html = generate_chargeback_summary(review_db, {})
    assert '$' not in html, 'Cost is labeled USD although no USD currency is configured'
    # The org's configured currency labels the cost; the unpriced job is counted, not valued as 0.
    assert 'EUR 4.50 (+1 unpriced)' in html
    review_db.execute(text("UPDATE jobs SET estimated_cost=NULL WHERE item_name='Cert priced'"))
    review_db.commit()
    assert '>Unknown<' in generate_chargeback_summary(review_db, {})


# ---------------- F10-10.9-03 toggle + recipients ----------------

def test_toggle_active_inactive_controls_daemon_delivery(admin_client, review_db):
    from modules.reporting import report_runner
    client, headers = admin_client
    identifier = _create(client, headers, name='Cert toggle', report_type='job_summary')
    review_db.execute(text("UPDATE report_schedules SET next_run_at='2000-01-01T08:00:00+00:00' WHERE id=:id"), {'id': identifier})
    review_db.commit()
    assert client.patch(f'/report-schedules/{identifier}', headers=headers, json={'is_active': False}).status_code == 200
    assert _row(client, headers, identifier)['is_active'] is False
    with SmtpPeer() as peer:
        _configure_smtp(review_db, peer)
        with Session(review_db.get_bind()) as session:
            report_runner.process_due_reports(session)
        assert peer.messages == []
        assert client.patch(f'/report-schedules/{identifier}', headers=headers, json={'is_active': True}).status_code == 200
        assert _row(client, headers, identifier)['is_active'] is True
        with Session(review_db.get_bind()) as session:
            report_runner.process_due_reports(session)
        assert peer.wait_for(1)[0]['rcpt_to'] == ['ops@example.test']
    assert client.patch(f'/report-schedules/{identifier}', headers=headers, json={'is_active': 'no'}).status_code == 400


def test_recipient_changes_drive_actual_envelope(admin_client, review_db):
    client, headers = admin_client
    identifier = _create(client, headers, recipients=['first@example.test'])
    assert client.patch(f'/report-schedules/{identifier}', headers=headers, json={'recipients': ['Second Person <second@example.test>', 'third@example.test']}).status_code == 200
    assert _row(client, headers, identifier)['recipients'] == ['second@example.test', 'third@example.test']
    with SmtpPeer() as peer:
        _configure_smtp(review_db, peer)
        assert client.post(f'/report-schedules/{identifier}/run', headers=headers).status_code == 200
        messages = peer.wait_for(2)
    assert sorted(m['rcpt_to'][0] for m in messages) == ['second@example.test', 'third@example.test']
    assert all(len(m['rcpt_to']) == 1 for m in messages)
    assert all(b'first@example.test' not in m['data'] for m in messages)


# ---------------- F10-10.9-05 report_runner daemon over SMTP ----------------

class _StopLoop(BaseException):
    pass


def test_daemon_main_loop_executes_due_schedule_and_delivers_html_over_smtp(review_db, monkeypatch):
    from modules.reporting import report_runner
    review_db.execute(text("INSERT INTO report_schedules(id,name,report_type,frequency,recipients,filters,is_active,next_run_at) "
                           "VALUES(301,'Cert daemon','fleet_utilization','weekly',:r,'{}',1,'2000-01-01T08:00:00+00:00'),"
                           "(302,'Cert future','fleet_utilization','daily',:r,'{}',1,'2999-01-01T08:00:00+00:00')"),
                       {'r': json.dumps(['daemon@example.test'])})
    review_db.commit()
    monkeypatch.setattr(report_runner, 'SessionLocal', sessionmaker(bind=review_db.get_bind()))
    monkeypatch.setattr(report_runner, 'process_quiet_hours_digest', lambda session: None)
    sleeps = []

    def stop(seconds):
        sleeps.append(seconds)
        raise _StopLoop()

    monkeypatch.setattr(report_runner.time, 'sleep', stop)
    with SmtpPeer() as peer:
        _configure_smtp(review_db, peer)
        with pytest.raises(_StopLoop):
            report_runner.main_loop()
        messages = peer.wait_for(1)
    assert sleeps == [report_runner.POLL_INTERVAL] == [60]
    assert len(messages) == 1
    message = messages[0]
    assert message['mail_from'] == 'odin-reports@cert.localhost' and message['rcpt_to'] == ['daemon@example.test']
    parsed, html = _html(message)
    assert parsed['Subject'] == 'O.D.I.N. Report: Cert daemon' and parsed['To'] == 'daemon@example.test'
    assert parsed.get_content_type() == 'multipart/alternative'
    assert 'Fleet Utilization Report' in html and '<table' in html
    with Session(review_db.get_bind()) as fresh:
        last, following = fresh.execute(text('SELECT last_run_at,next_run_at FROM report_schedules WHERE id=301')).one()
        assert last is not None and str(following) > str(last)
        assert fresh.execute(text('SELECT last_run_at FROM report_schedules WHERE id=302')).scalar_one() is None


def test_daemon_skips_when_smtp_disabled(review_db, monkeypatch):
    from modules.reporting import report_runner
    review_db.execute(text("INSERT INTO report_schedules(id,name,report_type,frequency,recipients,filters,is_active,next_run_at) "
                           "VALUES(303,'Cert disabled','job_summary','weekly',:r,'{}',1,'2000-01-01T08:00:00+00:00')"), {'r': json.dumps(['x@example.test'])})
    review_db.commit()
    with SmtpPeer() as peer:
        _configure_smtp(review_db, peer, enabled=False)
        with Session(review_db.get_bind()) as session:
            report_runner.process_due_reports(session)
        assert peer.commands == [] and peer.messages == []


# ---------------- F10-10.9-06 Run Now (backend) + UI producer fixture ----------------

def test_run_now_delivers_immediately_and_reports_partial_and_unconfigured(admin_client, review_db):
    client, headers = admin_client
    identifier = _create(client, headers, name='Cert weekly', recipients=['ops@example.test'])
    listing = client.get('/report-schedules', headers=headers).json()
    toggle = client.patch(f'/report-schedules/{identifier}', headers=headers, json={'is_active': True})
    # SMTP not configured: generation is refused with a safe message.
    unconfigured = client.post(f'/report-schedules/{identifier}/run', headers=headers)
    assert unconfigured.status_code == 400
    with SmtpPeer(refuse=['refused@example.test']) as peer:
        _configure_smtp(review_db, peer)
        sent = client.post(f'/report-schedules/{identifier}/run', headers=headers)
        assert sent.status_code == 200 and sent.json() == {'status': 'sent'}
        assert peer.wait_for(1)[0]['rcpt_to'] == ['ops@example.test']
        # Run Now does not consume or shift the schedule.
        assert _row(client, headers, identifier)['last_run_at'] is None
        assert client.patch(f'/report-schedules/{identifier}', headers=headers, json={'recipients': ['ops@example.test', 'refused@example.test']}).status_code == 200
        partial = client.post(f'/report-schedules/{identifier}/run', headers=headers)
        assert partial.status_code == 502
        assert partial.json()['detail'] == {'message': 'Report was not delivered to every recipient', 'sent_count': 1, 'failed_count': 1}
        assert len(peer.wait_for(2)) == 2
    missing = client.post('/report-schedules/999999/run', headers=headers)
    assert missing.status_code == 404
    fixture = {
        'scope': 'Captured from real reports router with hashed superadmin API token; delivery over local SMTP peer',
        'list': listing, 'toggle': toggle.json(),
        'run_sent': {'status': sent.status_code, 'body': sent.json()},
        'run_partial': {'status': partial.status_code, 'body': partial.json()},
        'run_unconfigured': {'status': unconfigured.status_code, 'body': unconfigured.json()},
    }
    _compare_fixture(fixture)


def _normalized(fixture):
    fixture = json.loads(json.dumps(fixture))
    for row in fixture['list']:
        for key in ('id', 'created_at', 'next_run_at'):
            row.pop(key, None)
    return fixture


def _compare_fixture(fixture):
    if FIXTURE.exists():
        assert _normalized(json.loads(FIXTURE.read_text())) == _normalized(fixture), 'Producer contract changed; reconcile the consumer explicitly'
    else:
        FIXTURE.write_text(json.dumps(fixture, indent=2) + '\n')
