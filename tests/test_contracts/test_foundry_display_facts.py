"""Foundry display facts from real monitor ingestion and scoped HTTP responses."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor
from tests.test_contracts.test_api_token_delegation_behavior import token_client


@pytest.fixture
def board(v2_offline_monitor, offline_store, token_client):
    from modules.printers.routes_crud import router as printers
    from modules.jobs.routes.jobs_crud import router as jobs
    client, issue = token_client
    client.app.include_router(printers)
    client.app.include_router(jobs)
    offline_store.execute(text("UPDATE printers SET slot_count=COALESCE(slot_count,4),tags=COALESCE(tags,'[]'),timelapse_enabled=COALESCE(timelapse_enabled,0),shared=COALESCE(shared,0)"))
    offline_store.execute(text("UPDATE printers SET model='H2D',machine_type='H2D' WHERE id=199"))
    offline_store.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,ams_mapping) VALUES(701,'Next print',199,1,'pending','[0,4]')"))
    offline_store.commit()
    return v2_offline_monitor[0], client, issue


def report(monitor, **extra):
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', **extra}}))


def get_printer(client, headers):
    response = client.get('/printers/199', headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_monitor_feeding_chamber_binding_and_timeline_to_http(board, offline_store):
    monitor, client, issue = board
    report(monitor, chamber_temper=38, ams={'tray_now': '4', 'ams': [
        {'id': '0', 'info': '100', 'tray': [{'id': '0', 'tray_type': 'PLA'}]},
        {'id': '1', 'info': '0', 'tray': [{'id': '0', 'tray_type': 'PLA'}]},
    ]})
    offline_store.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) VALUES"
        "('older',701,199,1,'bed_clear_confirmed','{}','2026-11-04T18:00:00+00:00'),"
        "('latest',701,199,1,'bed_clear_confirmed','{}','2026-11-04T18:31:00+00:00')"))
    offline_store.commit()
    headers = issue(['read:printers', 'read:jobs'])
    data = get_printer(client, headers)
    assert data['feeding_slot'] == 5
    assert data['chamber_temp'] == 38
    assert data['plate_clear_confirmed_at'].startswith('2026-11-04T18:31:00')
    listed = next(p for p in client.get('/printers', headers=headers).json() if p['id'] == 199)
    assert {k: listed[k] for k in ('feeding_slot','chamber_temp','plate_clear_confirmed_at')} == {k: data[k] for k in ('feeding_slot','chamber_temp','plate_clear_confirmed_at')}
    expected = [{'source_index': 0, 'slot_number': 1, 'nozzle': 'left'}, {'source_index': 1, 'slot_number': 5, 'nozzle': 'right'}]
    assert client.get('/jobs/701', headers=headers).json()['ams_slot_nozzles'] == expected
    assert next(j for j in client.get('/jobs', headers=headers).json() if j['id'] == 701)['ams_slot_nozzles'] == expected


@pytest.mark.parametrize('tray,expected', [('0',1),('15',16),('128',17),('135',24),('254',None),('255',None),('-1',None),('16',None),(True,None),('bad',None)])
def test_reported_tray_identity_and_unload(board, tray, expected):
    monitor, client, issue = board
    report(monitor, ams={'tray_now': '4'})
    report(monitor, ams={'tray_now': tray})
    assert get_printer(client, issue(['read:printers']))['feeding_slot'] == expected


@pytest.mark.parametrize('packet,expected', [({'chamber_temper': 0},0),({'chamber_temper':38},38),
    ({'device':{'ctc':{'info':{'temp':(60 << 16) | 41}}}},41),
    ({'chamber_temper':True},None),({'chamber_temper':float('nan')},None),({'chamber_temper':999},None),
    ({'device':{'ctc':{'info':{'temp':True}}}},None)])
def test_reported_chamber_formats_and_invalid_values(board, packet, expected):
    monitor, client, issue = board
    report(monitor, **packet)
    assert get_printer(client, issue(['read:printers']))['chamber_temp'] == expected


def test_partial_packets_preserve_fact_timestamps_and_expiry(board, offline_store):
    from core.printer_display_facts import cache_key
    monitor, client, issue = board
    report(monitor, chamber_temper=38, ams={'tray_now':'4'})
    old = json.loads(offline_store.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key':cache_key(199)}).scalar_one())
    report(monitor, ams={'humidity':'3'})
    current = json.loads(offline_store.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key':cache_key(199)}).scalar_one())
    assert current == old
    for fact in old['facts'].values():
        fact['at'] -= 301
    offline_store.execute(text('UPDATE system_config SET value=:value WHERE key=:key'), {'key':cache_key(199),'value':json.dumps(old)})
    offline_store.commit()
    data = get_printer(client, issue(['read:printers']))
    assert data['feeding_slot'] is None and data['chamber_temp'] is None


@pytest.mark.parametrize('condition', ['offline','retained','retired'])
def test_untrusted_or_offline_reports_do_not_show_live_facts(board, offline_store, condition):
    monitor, client, issue = board
    if condition == 'retired':
        monitor._connection_retired = True
    monitor._on_status(SimpleNamespace(raw_data={'print':{'chamber_temper':38,'ams':{'tray_now':'4'}}}, mqtt_retained=condition == 'retained'))
    if condition == 'offline':
        offline_store.execute(text("UPDATE printers SET gcode_state='OFFLINE' WHERE id=199")); offline_store.commit()
    data = get_printer(client, issue(['read:printers']))
    assert data['feeding_slot'] is None and data['chamber_temp'] is None


@pytest.mark.parametrize('path', ['/printers','/printers/199','/jobs','/jobs/701'])
def test_existing_routes_require_auth_and_correct_token_scope(board, path):
    _, client, issue = board
    assert client.get(path).status_code in (401,403)
    assert client.get(path, headers=issue(['read:jobs' if path.startswith('/printers') else 'read:printers'])).status_code == 403


def test_cross_org_printer_and_job_facts_remain_hidden(board, offline_store):
    monitor, client, issue = board
    report(monitor, chamber_temper=38, ams={'tray_now':'4'})
    offline_store.execute(text('UPDATE printers SET org_id=2 WHERE id=199'))
    offline_store.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=701')); offline_store.commit()
    headers = issue(['read:printers','read:jobs'])
    assert client.get('/printers/199',headers=headers).status_code == 404
    assert client.get('/jobs/701',headers=headers).status_code == 404
    assert 199 not in [p['id'] for p in client.get('/printers',headers=headers).json()]
    assert 701 not in [j['id'] for j in client.get('/jobs',headers=headers).json()]


def test_related_resource_permissions_and_hidden_plate_events(board, offline_store):
    monitor, client, issue = board
    report(monitor, ams={'ams':[{'id':'0','info':'100','tray':[{'id':'0','tray_type':'PLA'}]}]})
    offline_store.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) VALUES('clear',701,199,1,'bed_clear_confirmed','{}','2026-11-04T18:31:00+00:00')"));offline_store.commit()
    assert get_printer(client, issue(['read:printers']))['plate_clear_confirmed_at'] is None
    assert client.get('/jobs/701',headers=issue(['read:jobs'])).json()['ams_slot_nozzles'] == []
    offline_store.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=701'));offline_store.commit()
    assert get_printer(client,issue(['read:printers','read:jobs']))['plate_clear_confirmed_at'] is None


def test_mapping_without_binding_keeps_real_slot_and_omits_nozzle(board):
    _, client, issue = board
    result = client.get('/jobs/701',headers=issue(['read:printers','read:jobs'])).json()
    assert result['ams_slot_nozzles'] == [
        {'source_index':0,'slot_number':1,'nozzle':None}, {'source_index':1,'slot_number':5,'nozzle':None}]


def test_job_cannot_read_foreign_printer_binding_even_with_both_scopes(board, offline_store):
    monitor, client, issue = board
    report(monitor, ams={'ams':[{'id':'0','info':'100','tray':[{'id':'0','tray_type':'PLA'}]}]})
    offline_store.execute(text('UPDATE printers SET org_id=2 WHERE id=199'));offline_store.commit()
    headers = issue(['read:jobs','read:printers'])
    assert client.get('/jobs/701',headers=headers).json()['ams_slot_nozzles'] == []
    offline_store.execute(text('UPDATE printers SET shared=1 WHERE id=199'));offline_store.commit()
    assert client.get('/jobs/701',headers=headers).json()['ams_slot_nozzles'][0]['nozzle'] == 'left'


def test_pinned_unassigned_job_uses_existing_pin_columns(board, offline_store):
    _, client, issue = board
    offline_store.execute(text("UPDATE jobs SET printer_id=NULL,target_type='specific',target_filter='199' WHERE id=701"));offline_store.commit()
    assert client.get('/jobs/701',headers=issue(['read:jobs','read:printers'])).json()['ams_slot_nozzles'][0]['slot_number'] == 1


def test_shared_printer_confirmation_uses_latest_visible_job(board, offline_store):
    _, client, issue = board
    offline_store.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status) VALUES(702,'Foreign job',199,2,'pending')"))
    offline_store.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,occurred_at) VALUES"
        "('visible',701,199,1,'bed_clear_confirmed','{}','2026-11-04T18:00:00+00:00'),"
        "('hidden',702,199,2,'bed_clear_confirmed','{}','2026-11-04T18:31:00+00:00')"));offline_store.commit()
    assert get_printer(client,issue(['read:printers','read:jobs']))['plate_clear_confirmed_at'].startswith('2026-11-04T18:00:00')


def test_captured_h2d_report_contains_real_chamber_and_tray_binding():
    from pathlib import Path
    from core.printer_display_facts import observe
    capture = Path(__file__).resolve().parents[1] / 'fixtures/telemetry/bambu-h2d-failure-arc.jsonl'
    for line in capture.read_text().splitlines():
        packet = json.loads(line).get('payload', {}).get('print', {})
        if packet.get('device', {}).get('ctc', {}).get('info', {}).get('temp') is not None and packet.get('ams', {}).get('ams'):
            monitor = SimpleNamespace()
            observe(monitor, packet)
            assert monitor._display_facts['chamber_temp']['value'] == packet['device']['ctc']['info']['temp'] & 0xffff
            assert set(monitor._display_facts['slot_extruders']['value'].values()) <= {0, 1}
            assert monitor._display_facts['slot_extruders']['value']
            break
    else:
        pytest.fail('Capture is missing chamber/AMS observations')


@pytest.mark.parametrize('scopes', [['read:jobs'], ['read:jobs', 'read:printers']])
@pytest.mark.parametrize('pin,active,org,expected', [
    ('199', 1, 1, None),
    ('199', 0, 1, 'pinned_printer_inactive'),
    ('199', 1, 2, 'pinned_printer_inaccessible'),
    ('999999', 1, 1, 'pinned_printer_missing'),
    ('deleted:199', 1, 1, 'pinned_printer_deleted'),
    ('invalid', 1, 1, 'pinned_target_invalid'),
])
def test_job_list_reuses_printers_without_losing_pin_issues(board, offline_store, scopes, pin, active, org, expected):
    from sqlalchemy import event
    _, client, issue = board
    # The assignment differs from the pin. Pin lookup must not use the
    # display-facts `assignment or pin` shortcut or actor visibility.
    from modules.printers.models import Printer
    offline_store.add(Printer(id=198, name='Other assignment', model='A1', machine_type='A1',
                              api_type='bambu', org_id=1, is_active=True))
    offline_store.flush()
    offline_store.execute(text("UPDATE jobs SET printer_id=198,target_type='specific',target_filter=:pin WHERE id=701"), {'pin': pin})
    offline_store.execute(text('UPDATE printers SET is_active=:active,org_id=:org,shared=0 WHERE id=199'), {'active': active, 'org': org})
    offline_store.commit()
    seen = []
    def count(conn, cursor, statement, *args):
        if 'FROM printers' in statement and 'is_active' in statement:
            seen.append(statement)
    engine = offline_store.get_bind()
    event.listen(engine, 'before_cursor_execute', count)
    try:
        response = client.get('/jobs', headers=issue(scopes))
        assert response.status_code == 200, response.text
        job = next(j for j in response.json() if j['id'] == 701)
        assert job['pinned_printer_issue'] == expected
        assert job['ams_slot_nozzles'] == []
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(seen) == 1, seen
