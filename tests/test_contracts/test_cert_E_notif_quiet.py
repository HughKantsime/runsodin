"""Cert-E quiet hours: real HTTP org configuration, real channels, real digest.

Claims F13-13.2-01..04 and F10-10.9-07. Org quiet hours and the org webhook are
configured through the real ``PUT /orgs/{id}/settings`` route (SSRF validation and
Fernet encryption at rest). Alerts are produced by both production dispatchers at
the real wall clock inside the configured window, so suppression uses the real
``should_suppress_notification`` clock. The digest is produced by the real
``report_runner.process_quiet_hours_digest`` with only the quiet-hours module clock
moved past the window end. Outbound traffic goes to real local peers: HTTP receiver
(org webhook + Web Push service), SMTP protocol peer.
"""
import json
from datetime import datetime, timedelta, timezone

import http_ece
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_channels import (  # noqa: F401
    admin_api, configure_smtp, dispatch, notif_env, receiver, review_db, signed_license, smtp_peer, synthetic_dns)
from tests.test_contracts.test_cert_E_notif_push import (  # noqa: F401
    b64u, browser, push_api, subscribe, vapid, verify_vapid_jwt)


def window(offset_minutes=0):
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=offset_minutes)
    return base, (base - timedelta(minutes=30)).strftime('%H:%M'), (base + timedelta(minutes=30)).strftime('%H:%M')


@pytest.fixture
def org_api(notif_env, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations import routes
    from modules.organizations.services import OrgSettingsService
    monkeypatch.setitem(registry._providers, 'OrgSettingsProvider', OrgSettingsService())
    app = FastAPI()
    app.include_router(routes.router)

    def session():
        with Session(notif_env.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None}
    notif_env.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,org_id) VALUES(11,'P1S-11','P1S',1,'bambu',2)"))
    notif_env.commit()
    with TestClient(app) as client:
        yield client


def put_org(org_api, org_id, **settings):
    response = org_api.put(f'/orgs/{org_id}/settings', json=settings)
    assert response.status_code == 200, response.text
    return response.json()


def prefs(db, user_id, alert_type, in_app=True, email=False, push=False):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    db.add(AlertPreference(user_id=user_id, alert_type=AlertType(alert_type), in_app=in_app, email=email, browser_push=push))
    db.commit()


def move_quiet_clock(monkeypatch, moment):
    from modules.notifications import quiet_hours

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz else moment.replace(tzinfo=None)
    monkeypatch.setattr(quiet_hours, 'datetime', Clock)


def run_digest(db):
    from modules.reporting import report_runner
    with Session(db.get_bind()) as session:
        report_runner.process_quiet_hours_digest(session)


# --------------------------------------------------------------------------
# Suppression during the window (F13-13.2-01/02/03, F10-10.9-07)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['orm', 'daemon'])
@pytest.mark.parametrize('inside', [True, False])
def test_http_configured_org_window_suppresses_external_channels_and_keeps_in_app(
        org_api, admin_api, push_api, notif_env, receiver, synthetic_dns, smtp_peer, vapid, browser, path, inside):
    _, start, end = window(0 if inside else 120)
    receiver.status = 201
    stored = put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end,
                     webhook_url=f'{synthetic_dns}/org1', webhook_type='generic')
    assert stored['quiet_hours_enabled'] is True and (stored['quiet_hours_start'], stored['quiet_hours_end']) == (start, end)
    raw = json.loads(notif_env.execute(text('SELECT settings_json FROM groups WHERE id=1')).scalar_one())
    assert raw['webhook_url'].startswith('gAAAA')  # org webhook encrypted at rest
    put_org(org_api, 2, quiet_hours_enabled=False, webhook_url=f'{synthetic_dns}/org2-sentinel', webhook_type='generic')
    configure_smtp(admin_api, smtp_peer)
    subscribe(push_api, receiver, browser)
    prefs(notif_env, 1, 'print_failed', in_app=True, email=True, push=True)
    prefs(notif_env, 5, 'print_failed', in_app=True)
    dispatch_printer(path, 9, notif_env)
    dispatch_printer(path, 11, notif_env)  # sibling org outside quiet hours: sentinel
    if inside:
        request, = receiver.wait(1)
        assert request['path'] == '/org2-sentinel'
        assert smtp_peer.messages == []
    else:
        # ORM-path push is pinned separately (test_cert_E_notif_push: VAPID key source defect).
        expected = {'/org1', '/org2-sentinel'} | ({'/push/sub-1'} if path == 'daemon' else set())
        requests = receiver.wait(len(expected))
        assert {r['path'] for r in requests} == expected
        assert smtp_peer.wait(1)[0]['To'] == 'student@example.test'
    with notif_env.get_bind().connect() as conn:
        rows = conn.execute(text('SELECT user_id,printer_id,alert_type FROM alerts ORDER BY printer_id,user_id')).all()
    assert [tuple(r) for r in rows] == [(1, 9, 'print_failed'), (5, 11, 'print_failed')]


def dispatch_printer(path, printer_id, db):
    from core.base import AlertSeverity, AlertType
    from modules.notifications import alert_dispatch, alert_dispatcher
    title = f'Synthetic failure on {printer_id}'
    if path == 'orm':
        alert_dispatcher.dispatch_alert(db, AlertType.PRINT_FAILED, AlertSeverity.CRITICAL, title, 'Details', printer_id=printer_id)
    else:
        alert_dispatch.dispatch_alert('print_failed', 'critical', title, 'Details', printer_id=printer_id)


# --------------------------------------------------------------------------
# Digest after the window (F13-13.2-04, F10-10.9-07)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_window_end_digest_per_user_email_and_push_respect_preferences(
        org_api, admin_api, push_api, notif_env, receiver, smtp_peer, vapid, browser, monkeypatch, path):
    base, start, end = window()
    receiver.status = 201
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)
    configure_smtp(admin_api, smtp_peer)
    subscribe(push_api, receiver, browser)
    prefs(notif_env, 1, 'print_failed', email=True, push=True)
    prefs(notif_env, 1, 'print_complete', email=True, push=False)
    prefs(notif_env, 3, 'print_failed', email=False, push=False)  # in-app only
    from core.base import AlertSeverity, AlertType
    from modules.notifications import alert_dispatch, alert_dispatcher
    for kind, title in [('print_failed', 'Synthetic quiet failure'), ('print_complete', 'Synthetic quiet complete')]:
        if path == 'orm':
            alert_dispatcher.dispatch_alert(notif_env, AlertType(kind), AlertSeverity.INFO, title, 'x', printer_id=9)
        else:
            alert_dispatch.dispatch_alert(kind, 'info', title, 'x', printer_id=9)
    assert smtp_peer.messages == [] and receiver.requests == []  # suppressed in real time
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    message, = smtp_peer.wait(1)
    assert message['To'] == 'student@example.test'
    assert str(message['Subject']).startswith('O.D.I.N. Quiet Hours Digest — 2 alerts (window ended ')
    html = message.get_body(preferencelist=('html',)).get_content()
    assert 'Synthetic quiet failure' in html and 'Synthetic quiet complete' in html
    request, = receiver.wait(1)
    verify_vapid_jwt(request['headers']['authorization'], vapid['public_key'], f'http://127.0.0.1:{receiver.port}')
    payload = json.loads(http_ece.decrypt(request['body'], private_key=browser['private'],
                                          auth_secret=browser['auth_raw'], version='aes128gcm'))
    assert payload['title'] == '1 alert during quiet hours' and payload['alert_type'] == 'quiet_hours_digest'
    with notif_env.get_bind().connect() as conn:
        claims = dict(conn.execute(text('SELECT user_id,delivery_status FROM quiet_hours_digest_sends')).all())
    assert claims == {1: 'sent', 3: 'failed:no_enabled_channel'}
    # Next poll within the same ended window: no duplicate sends.
    run_digest(notif_env)
    assert len(smtp_peer.messages) == 1 and len(receiver.requests) == 1


def test_window_end_org_digest_webhook_configured_over_http(org_api, notif_env, receiver, synthetic_dns, monkeypatch):
    base, start, end = window()
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end,
            webhook_url=f'{synthetic_dns}/digest', webhook_type='generic')
    prefs(notif_env, 1, 'print_failed')
    dispatch_printer('daemon', 9, notif_env)
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    request, = receiver.wait(1, timeout=1)
    body = json.loads(request['body'])
    assert body['event'] == 'quiet_hours_digest' and body['alert_count'] == 1


def legacy_plaintext_org(db, start, end, url, kind):
    db.execute(text('UPDATE groups SET settings_json=:s WHERE id=1'), {'s': json.dumps({
        'quiet_hours_enabled': True, 'quiet_hours_start': start, 'quiet_hours_end': end,
        'webhook_url': url, 'webhook_type': kind})})
    db.commit()


def test_org_digest_webhook_combined_payload_with_plaintext_url(org_api, notif_env, receiver, synthetic_dns, monkeypatch):
    base, start, end = window()
    legacy_plaintext_org(notif_env, start, end, f'{synthetic_dns}/digest', 'generic')
    prefs(notif_env, 1, 'print_failed')
    prefs(notif_env, 3, 'print_failed')
    dispatch_printer('daemon', 9, notif_env)
    assert receiver.requests == []
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    run_digest(notif_env)
    request, = receiver.wait(1)
    body = json.loads(request['body'])
    assert body['event'] == 'quiet_hours_digest' and body['org'] == 'school'
    assert body['alert_count'] == 2 and body['truncated'] is False  # one combined dispatch for both users' alerts
    assert {a['title'] for a in body['alerts']} == {'Synthetic failure on 9'}
    assert len(receiver.requests) == 1
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text('SELECT delivery_status FROM quiet_hours_org_digest_sends')).scalar_one() == 'sent'


def test_org_digest_webhook_uses_discord_payload_for_discord_org(org_api, notif_env, receiver, synthetic_dns, monkeypatch):
    base, start, end = window()
    legacy_plaintext_org(notif_env, start, end, f'{synthetic_dns}/discord', 'discord')
    prefs(notif_env, 1, 'print_failed')
    dispatch_printer('daemon', 9, notif_env)
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    body = json.loads(receiver.wait(1)[0]['body'])
    assert 'embeds' in body or 'content' in body
    embed, = body['embeds']
    assert '1 alert' in embed['title'] and 'Synthetic failure on 9' in embed['description']
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text('SELECT delivery_status FROM quiet_hours_org_digest_sends')).scalar_one() == 'sent'


def test_org_digest_webhook_uses_slack_payload_for_slack_org(org_api, notif_env, receiver, synthetic_dns, monkeypatch):
    base, start, end = window()
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end,
            webhook_url=f'{synthetic_dns}/slack', webhook_type='slack')
    prefs(notif_env, 1, 'print_failed')
    dispatch_printer('daemon', 9, notif_env)
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    request, = receiver.wait(1)
    body = json.loads(request['body'])
    assert request['path'] == '/slack' and body['blocks'][0]['type'] == 'header'
    assert 'Synthetic failure on 9' in body['blocks'][1]['text']['text']


def test_org_digest_webhook_http_error_recorded_as_failed(org_api, notif_env, receiver, synthetic_dns, monkeypatch):
    base, start, end = window()
    receiver.status = 500
    legacy_plaintext_org(notif_env, start, end, f'{synthetic_dns}/digest', 'generic')
    prefs(notif_env, 1, 'print_failed')
    dispatch_printer('daemon', 9, notif_env)
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    receiver.wait(1)
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text('SELECT delivery_status FROM quiet_hours_org_digest_sends')).scalar_one().startswith('failed')


@pytest.mark.xfail(strict=True, reason=(
    'DEFECT: backend/modules/notifications/alert_dispatcher.py:406 (and alert_dispatch.py:61) record an in-app alert '
    'only for users whose preference has in_app enabled, and the digest (quiet_hours.py:285 '
    'get_suppressed_alerts_for_window) reads only the alerts table; an email/push-only user loses every alert '
    'suppressed during quiet hours'))
@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_email_only_user_receives_digest_of_suppressed_alert(
        org_api, admin_api, notif_env, smtp_peer, monkeypatch, path):
    base, start, end = window()
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)
    configure_smtp(admin_api, smtp_peer)
    prefs(notif_env, 1, 'print_failed', in_app=False, email=True)
    dispatch_printer(path, 9, notif_env)
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    smtp_peer.wait(1, timeout=1)


def test_system_scope_global_webhooks_receive_typed_digest(admin_api, notif_env, receiver, synthetic_dns, monkeypatch):
    """System-wide quiet hours suppress the global webhook channels (webhooks table);
    after the window each selecting webhook receives one digest in its own payload shape."""
    base, start, end = window()
    from modules.notifications import quiet_hours
    for key, value in [('quiet_hours_enabled', 'true'), ('quiet_hours_start', start), ('quiet_hours_end', end)]:
        notif_env.execute(text('INSERT INTO system_config(key,value) VALUES(:k,:v)'), {'k': key, 'v': value})
    notif_env.commit()
    quiet_hours.invalidate_cache()
    assert admin_api.post('/webhooks', json={'name': 'Global', 'url': f'{synthetic_dns}/global', 'webhook_type': 'discord',
                                             'alert_types': ['print_failed']}).status_code == 200
    # A webhook that does not select the event gets no digest.
    assert admin_api.post('/webhooks', json={'name': 'Other', 'url': f'{synthetic_dns}/other', 'webhook_type': 'generic',
                                             'alert_types': ['print_complete']}).status_code == 200
    prefs(notif_env, 1, 'print_failed')
    dispatch('daemon', 'print_failed', 'critical', 'Synthetic system failure', 'x', notif_env)
    assert receiver.requests == []  # suppressed in real time
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    quiet_hours.invalidate_cache()
    run_digest(notif_env)
    run_digest(notif_env)  # same ended window: no duplicate
    request, = receiver.wait(1)
    assert request['path'] == '/global'
    embed, = json.loads(request['body'])['embeds']
    assert 'Synthetic system failure' in embed['description']
    assert len(receiver.requests) == 1
    with notif_env.get_bind().connect() as conn:
        rows = conn.execute(text('SELECT org_id, scope_key, delivery_status FROM quiet_hours_org_digest_sends')).all()
        assert [tuple(r) for r in rows] == [(None, 'webhook:1', 'sent')]
        # The suppressed alert itself was recorded in-app.
        assert conn.execute(text("SELECT count(*) FROM alerts WHERE title='Synthetic system failure'")).scalar_one() == 1
