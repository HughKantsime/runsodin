"""Home Assistant status endpoint: read-only, database only, scoped like the printer list.

Spec: .claude/specs/home-assistant-integration.md (Part A).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers, token_client, review_db  # noqa: F401

URL = '/integrations/home-assistant/status'


def _stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(tzinfo=None).isoformat(sep=' ')


@pytest.fixture
def ha_client(token_client, review_db, monkeypatch):
    from modules.printers import routes_integrations, routes_status
    client, issue = token_client
    client.app.include_router(routes_integrations.router)

    def no_printer_io(*args, **kwargs):
        raise AssertionError('the Home Assistant status route must not contact printers')
    monkeypatch.setattr(routes_status, '_fetch_printer_live_status', no_printer_io)
    import modules.printers.telemetry.bambu.session as bambu_session
    monkeypatch.setattr(bambu_session, 'read_status_once', no_printer_io)

    review_db.execute(text("UPDATE printers SET gcode_state='RUNNING', last_seen=:seen, "
                           "api_host='printer-host.example.test', api_key='synthetic-secret-credential' WHERE id=9"),
                      {'seen': _stamp(5)})
    review_db.execute(text("UPDATE printers SET gcode_state='IDLE', last_seen=:seen, "
                           "hms_errors='[{\"attr\": 1, \"code\": 2, \"raw_code\": 2}]' WHERE id=10"),
                      {'seen': _stamp(5)})
    review_db.execute(text("INSERT INTO printers(id,name,model,api_type,is_active,org_id,gcode_state,last_seen,"
                           "last_error_code,last_error_message,last_error_at) VALUES "
                           "(12,'Kobra S1','Anycubic Kobra S1','anycubic_lan',1,NULL,'PAUSED',:seen,'PRINT_FAILED','Nozzle clog',:err),"
                           "(71,'Foreign printer','Bambu Lab A1','bambu',1,2,'RUNNING',:seen,NULL,NULL,NULL),"
                           "(72,'Stale printer','Bambu Lab A1','bambu',1,1,'RUNNING',:stale,NULL,NULL,NULL),"
                           "(73,'Retired printer','Bambu Lab A1','bambu',0,1,'IDLE',:seen,NULL,NULL,NULL)"),
                      {'seen': _stamp(5), 'stale': _stamp(600), 'err': '2026-11-08 23:36:50'})
    review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,progress_percent,"
                           "current_layer,total_layers,remaining_minutes) VALUES "
                           "(9,'synthetic-part.gcode','running',:start,42.5,12,118,90),"
                           "(71,'foreign-part.gcode','running',:start,10,1,10,5),"
                           "(10,'old-part.gcode','completed',:start,100,10,10,0)"),
                      {'start': _stamp(3600)})
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES "
                           "(301,'Queued owned','pending',1),(302,'Queued global','scheduled',NULL),"
                           "(303,'Queued foreign','submitted',2),(304,'Done owned','completed',1)"))
    review_db.commit()
    return client, issue


def test_status_payload_shape_and_values(ha_client):
    client, issue = ha_client
    response = client.get(URL, headers=issue(['read:printers', 'read:jobs']))
    assert response.status_code == 200
    body = response.json()
    assert body['schema_version'] == 1
    assert body['odin_version']
    printers = {p['id']: p for p in body['printers']}
    # Org 1, global, never the foreign org or an inactive printer.
    assert set(printers) == {9, 10, 12, 72}
    nine = printers[9]
    assert nine['status'] == 'printing' and nine['online'] is True and nine['raw_state'] == 'RUNNING'
    assert (nine['progress'], nine['current_layer'], nine['total_layers'], nine['remaining_minutes']) == (42.5, 12, 118, 90.0)
    eta = datetime.fromisoformat(nine['eta'].replace('Z', '+00:00'))
    assert timedelta(minutes=89) < eta - datetime.now(timezone.utc) < timedelta(minutes=91)
    assert nine['job_name']
    assert printers[10]['status'] == 'error' and printers[10]['active_errors'] == 1
    assert printers[10]['progress'] is None and printers[10]['job_name'] is None
    kobra = printers[12]
    assert kobra['api_type'] == 'anycubic_lan' and kobra['status'] == 'paused'
    assert kobra['progress'] is None and kobra['eta'] is None
    assert kobra['last_error'] == {'code': 'PRINT_FAILED', 'message': 'Nozzle clog', 'at': '2026-11-08T23:36:50Z'}
    assert printers[72]['status'] == 'offline' and printers[72]['online'] is False
    assert body['fleet'] == {'printing': 1, 'paused': 1, 'error': 1, 'offline': 1, 'idle': 0,
                             'total': 4, 'queue_length': 3}  # 301, 302 and the seeded submitted job 13


def test_payload_carries_no_credentials_hosts_or_camera_urls(ha_client):
    client, issue = ha_client
    raw = client.get(URL, headers=issue(['read'])).text
    for secret in ('synthetic-secret-credential', 'printer-host.example.test', 'api_key', 'api_host', 'camera_url', 'rtsp'):
        assert secret not in raw


@pytest.mark.parametrize('scopes,status', [
    (['read:printers', 'read:jobs'], 200), (['read'], 200), (['admin'], 200),
    (['read:printers'], 403), (['read:jobs'], 403), (['write'], 403), (['write:printers', 'write:jobs'], 403),
    (['agent:read'], 403), (['read:cameras'], 401),
])
def test_token_scope_matrix(ha_client, scopes, status):
    client, issue = ha_client
    assert client.get(URL, headers=issue(scopes)).status_code == status


def test_interactive_session_reads_status(ha_client):
    client, _ = ha_client
    assert client.get(URL, headers=interactive_headers()).status_code == 200


def test_unauthenticated_is_refused(ha_client):
    client, _ = ha_client
    assert client.get(URL).status_code == 401


def test_superadmin_sees_every_active_printer_and_job(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    body = client.get(URL, headers=issue(['read'])).json()
    assert {p['id'] for p in body['printers']} == {9, 10, 12, 71, 72}
    assert body['fleet']['queue_length'] == 4  # adds the foreign org's 303


def test_shared_foreign_printer_is_visible(ha_client, review_db):
    client, issue = ha_client
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=71'))
    review_db.commit()
    assert 71 in {p['id'] for p in client.get(URL, headers=issue(['read'])).json()['printers']}


def test_controlled_work_names_are_redacted(ha_client, monkeypatch):
    import core.controlled_parts as controlled
    client, issue = ha_client
    monkeypatch.setattr(controlled, 'allowed', lambda *args, **kwargs: False)
    printers = {p['id']: p for p in client.get(URL, headers=issue(['read'])).json()['printers']}
    assert printers[9]['job_name'] == 'Controlled work'


@pytest.mark.parametrize('state,online,hms,expected', [
    ('RUNNING', True, 0, 'printing'), ('PRINTING', True, 3, 'printing'),
    ('PAUSE', True, 1, 'paused'), ('PAUSED', True, 0, 'paused'),
    ('FAILED', True, 0, 'error'), ('ERROR', True, 0, 'error'), ('IDLE', True, 1, 'error'),
    ('FINISH', True, 0, 'idle'), ('IDLE', True, 0, 'idle'), (None, True, 0, 'idle'),
    ('RUNNING', False, 0, 'offline'), ('FAILED', False, 2, 'offline'),
])
def test_status_derivation_table(state, online, hms, expected):
    from modules.printers.routes_integrations import derive_status
    assert derive_status(state, online, hms) == expected


def test_online_threshold_and_offline_state():
    from modules.printers.routes_integrations import is_online
    now = datetime.now(timezone.utc)
    assert is_online('IDLE', now - timedelta(seconds=89), now)
    assert not is_online('IDLE', now - timedelta(seconds=91), now)
    assert not is_online('OFFLINE', now, now)
    assert not is_online('IDLE', None, now)
    assert not is_online('IDLE', now + timedelta(seconds=30), now)
    assert is_online('IDLE', (now - timedelta(seconds=5)).replace(tzinfo=None).isoformat(sep=' '), now)
