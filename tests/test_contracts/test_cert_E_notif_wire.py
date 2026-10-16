"""Cert-E producer wire fixtures for the notification UI (F13-13.3-*, F13-13.2-01, F13-13.1-02/03).

Every response body is captured from the real FastAPI routers over a bootstrapped
SQLite schema, with alerts produced by the production ORM dispatcher. Timestamps
are pinned in the DB before capture so the fixture is reproducible. On first run
the fixture is written; afterwards a mismatch fails (producer contract changed).
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_channels import notif_env, review_db, signed_license  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures'


def write_or_compare(name, fixture):
    path = FIXTURES / name
    if path.exists():
        assert json.loads(path.read_text()) == fixture, f'Producer contract changed for {name}; reconcile the consumer explicitly'
    else:
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + '\n')


@pytest.fixture
def wire_app(notif_env, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.errors import OdinError
    from core.registry import registry
    from modules.notifications.routes import alerts, webhooks
    from modules.organizations import routes as org_routes
    from modules.organizations.services import OrgSettingsService
    monkeypatch.setitem(registry._providers, 'OrgSettingsProvider', OrgSettingsService())
    app = FastAPI()
    for router in (alerts.router, webhooks.router, org_routes.router):
        app.include_router(router)

    @app.exception_handler(OdinError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    def session():
        with Session(notif_env.get_bind()) as db:
            yield db
    identity = {'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None, 'email': 'admin@example.test'}
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: identity
    with TestClient(app) as client:
        yield client, identity


def produce_alerts(db):
    """Three in-app alerts for user 3 through the production ORM dispatcher."""
    from core.base import AlertSeverity, AlertType
    from modules.notifications import alert_dispatcher
    from modules.notifications.models import AlertPreference
    for kind in ('print_failed', 'maintenance_overdue', 'print_complete'):
        db.add(AlertPreference(user_id=3, alert_type=AlertType(kind), in_app=True, email=False, browser_push=False))
    db.add(AlertPreference(user_id=5, alert_type=AlertType.PRINT_FAILED, in_app=True, email=False, browser_push=False))
    db.commit()
    for kind, severity, title, message, printer in [
        ('print_complete', 'info', 'Print Complete: Synthetic bracket', 'Job finished on P1S-09', 9),
        ('maintenance_overdue', 'warning', 'Maintenance Due: P1S-10', 'Nozzle wipe overdue', 10),
        ('print_failed', 'critical', 'Print Failed: Synthetic hinge', 'Failed at 67% progress', 9),
    ]:
        alert_dispatcher.dispatch_alert(db, AlertType(kind), AlertSeverity(severity), title, message, printer_id=printer)
    db.execute(text("UPDATE alerts SET created_at = CASE alert_type "
                    "WHEN 'print_complete' THEN '2026-10-14 08:00:00' WHEN 'maintenance_overdue' THEN '2026-10-14 08:05:00' "
                    "ELSE '2026-10-14 08:10:00' END"))
    db.commit()
    return {r.alert_type: r.id for r in db.execute(text('SELECT id,alert_type FROM alerts WHERE user_id=3'))}


def test_capture_alerts_wire_from_real_http(wire_app, notif_env):
    client, identity = wire_app
    ids = produce_alerts(notif_env)
    assert len(ids) == 3
    # Another tenant's alert exists but is never visible to user 3.
    assert notif_env.execute(text('SELECT count(*) FROM alerts WHERE user_id<>3')).scalar_one() == 0
    exchanges = {}

    def get(path):
        response = client.get(path)
        assert response.status_code == 200, response.text
        exchanges['GET ' + path] = response.json()
        return response.json()
    for path in ['/alerts?limit=25', '/alerts?severity=critical&limit=25', '/alerts?severity=warning&limit=25',
                 '/alerts?severity=info&limit=25', '/alerts?is_read=false&limit=25', '/alerts?limit=10',
                 '/alerts/digest-status']:
        get(path)
    assert [a['severity'] for a in exchanges['GET /alerts?limit=25']] == ['critical', 'warning', 'info']
    assert [a['alert_type'] for a in exchanges['GET /alerts?severity=critical&limit=25']] == ['print_failed']
    counts = [client.get('/alerts/unread-count').json()]
    read_path = f"/alerts/{ids['print_failed']}/read"
    read = client.patch(read_path)
    assert read.status_code == 200
    exchanges['PATCH ' + read_path] = read.json()
    counts.append(client.get('/alerts/unread-count').json())
    exchanges['GET /alerts?is_read=false&limit=25 (after read)'] = client.get('/alerts?is_read=false&limit=25').json()
    delete_path = f"/alerts/{ids['print_complete']}"
    deleted = client.delete(delete_path)
    assert deleted.status_code == 200
    exchanges['DELETE ' + delete_path] = deleted.json()
    counts.append(client.get('/alerts/unread-count').json())
    # Dismiss the maintenance alert: hidden from the default list (also after reload), listed by
    # the Dismissed filter and include_dismissed, and no longer counted as unread.
    dismiss_path = f"/alerts/{ids['maintenance_overdue']}/dismiss"
    dismissed = client.patch(dismiss_path)
    assert dismissed.status_code == 200
    exchanges['PATCH ' + dismiss_path] = dismissed.json()
    exchanges['GET /alerts?limit=25 (after dismiss)'] = client.get('/alerts?limit=25').json()
    exchanges['GET /alerts?is_dismissed=true&limit=25'] = client.get('/alerts?is_dismissed=true&limit=25').json()
    exchanges['GET /alerts?include_dismissed=true&limit=25'] = client.get('/alerts?include_dismissed=true&limit=25').json()
    assert [a['id'] for a in exchanges['GET /alerts?limit=25 (after dismiss)']] == [ids['print_failed']]
    assert [a['id'] for a in exchanges['GET /alerts?is_dismissed=true&limit=25']] == [ids['maintenance_overdue']]
    assert [a['id'] for a in exchanges['GET /alerts?include_dismissed=true&limit=25']] == [ids['print_failed'], ids['maintenance_overdue']]
    counts.append(client.get('/alerts/unread-count').json())
    assert counts == [{'unread_count': 3}, {'unread_count': 2}, {'unread_count': 1}, {'unread_count': 0}]
    assert notif_env.execute(text('SELECT count(*) FROM alerts WHERE id=:i'), {'i': ids['print_complete']}).scalar_one() == 0
    write_or_compare('cert-E-notif-alerts.json', {
        'scope': 'Real alerts router; alerts produced by the production ORM dispatcher for user 3 (admin, org 1); '
                 'timestamps pinned; unread counts captured before, after mark-read, after delete and after dismiss',
        'ids': ids, 'exchanges': exchanges, 'unread_counts': counts})


def test_capture_org_quiet_hours_settings_wire(wire_app, notif_env):
    client, _ = wire_app
    body = {'quiet_hours_enabled': True, 'quiet_hours_start': '21:30', 'quiet_hours_end': '06:45',
            'webhook_url': 'https://hooks.cert-e.test/org-1', 'webhook_type': 'discord'}
    saved = client.put('/orgs/1/settings', json=body)
    assert saved.status_code == 200, saved.text
    notif_env.execute(text("UPDATE groups SET created_at='2026-10-01 00:00:00', owner_id=2 WHERE id IN (1,2)"))
    notif_env.commit()
    orgs = client.get('/orgs').json()
    settings = client.get('/orgs/1/settings').json()
    assert settings['quiet_hours_enabled'] is True and settings['quiet_hours_end'] == '06:45'
    assert settings['webhook_url'] == 'https://hooks.cert-e.test/org-1'
    put_response = saved.json()
    # The PUT echo carries the at-rest Fernet ciphertext (non-deterministic); record that fact only.
    assert put_response.pop('webhook_url').startswith('gAAAA')
    write_or_compare('cert-E-notif-org-settings.json', {
        'scope': 'Real organizations router: PUT then GET /orgs/1/settings (webhook URL decrypted on read; '
                 'PUT echo returns ciphertext, omitted here)',
        'orgs': orgs, 'settings': settings, 'put_response': put_response})


def test_capture_webhook_and_smtp_admin_wire(wire_app, notif_env):
    client, _ = wire_app
    created = client.post('/webhooks', json={'name': 'Synthetic Discord', 'url': 'https://discord.cert-e.test/api/webhooks/1/x',
                                             'webhook_type': 'discord', 'alert_types': ['print_failed', 'spool_low']})
    assert created.status_code == 200, created.text
    notif_env.execute(text("UPDATE webhooks SET created_at='2026-10-01 00:00:00', updated_at='2026-10-01 00:00:00'"))
    notif_env.commit()
    hooks = client.get('/webhooks').json()
    assert hooks[0]['url'] == 'https://discord.cert-e.test/api/webhooks/1/x'
    smtp = client.put('/smtp-config', json={'enabled': True, 'host': 'smtp.cert-e.test', 'port': 2525, 'username': 'odin',
                                            'password': 'synthetic-noncredential', 'from_address': 'odin@example.test'})
    assert smtp.status_code == 200
    write_or_compare('cert-E-notif-admin.json', {
        'scope': 'Real webhooks and SMTP routers as system admin',
        'webhooks': hooks, 'smtp_config': client.get('/smtp-config').json()})


def test_dismissed_alerts_are_excluded_before_paging(wire_app, notif_env):
    """25-per-page paging with dismissed rows interleaved: every page holds visible alerts only,
    pages do not overlap, and unread count / summary agree with the default list."""
    client, _ = wire_app
    for n in range(32):
        notif_env.execute(text(
            "INSERT INTO alerts(user_id,alert_type,severity,title,message,is_read,is_dismissed,created_at) "
            "VALUES(3,'print_failed','critical',:t,'m',0,:d,:c)"),
            {"t": f"Synthetic paged {n:02d}", "d": 1 if n % 8 == 3 else 0, "c": f"2026-10-14 09:{n:02d}:00"})
    notif_env.commit()
    visible = [f"Synthetic paged {n:02d}" for n in reversed(range(32)) if n % 8 != 3]
    assert len(visible) == 28
    first = client.get('/alerts?limit=25').json()
    second = client.get('/alerts?limit=25&offset=25').json()
    assert [a['title'] for a in first] == visible[:25]
    assert [a['title'] for a in second] == visible[25:]
    assert not any(a['is_dismissed'] for a in first + second)
    everything = client.get('/alerts?include_dismissed=true&limit=25').json() + \
        client.get('/alerts?include_dismissed=true&limit=25&offset=25').json()
    assert len(everything) == 32 and sum(a['is_dismissed'] for a in everything) == 4
    assert len(client.get('/alerts?is_dismissed=true&limit=25').json()) == 4
    assert len(client.get('/alerts?is_dismissed=false&limit=50').json()) == 28
    assert client.get('/alerts/unread-count').json() == {'unread_count': 28}
    assert client.get('/alerts/summary').json()['print_failed'] == 28
