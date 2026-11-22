"""Captured AMS 3H reports through the monitor and authenticated customer routes."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import review_db, token_client
from tests.test_contracts.test_h3c_readonly import capture_packet, distinguish_redacted_tags
from tests.test_contracts.test_printer_offline_behavior import offline_store, v2_offline_monitor


@pytest.fixture
def inventory(v2_offline_monitor, offline_store, token_client, monkeypatch):
    from modules.notifications import error_handling
    from modules.printers import routes_crud, routes_status
    monitor, _, runtime = v2_offline_monitor
    monkeypatch.setattr(error_handling, 'get_db', runtime.get_db)
    alerts = []
    monkeypatch.setattr(error_handling, 'dispatch_alert', lambda **kwargs: alerts.append(kwargs))
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    client.app.include_router(routes_status.router)
    headers = issue(['read:printers'])

    def send(packet, at=2000):
        monkeypatch.setattr(runtime, 'time', SimpleNamespace(time=lambda: at))
        monitor._on_status(SimpleNamespace(raw_data={'print': packet}))

    def view(path='/printers/199'):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        return response.json()

    return send, view, alerts


@pytest.mark.parametrize('model', [None, 'P1S'])
def test_unit_32_needs_an_identified_model(inventory, offline_store, model):
    send, view, _ = inventory
    offline_store.execute(text('UPDATE printers SET model=:model WHERE id=199'), {'model': model})
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex,do_not_feed) "
                               "VALUES(199,1,'PETG','123456',true)"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    distinguish_redacted_tags(capture)
    before = view()['filament_slots']
    send(capture)
    assert view()['filament_slots'] == before
    assert offline_store.execute(text('SELECT count(*) FROM ams_telemetry WHERE printer_id=199')).scalar_one() == 0


def test_tray_changes_and_hand_feed_use_separate_sources(inventory, offline_store):
    send, view, _ = inventory
    capture = capture_packet('ams3h-loaded')
    distinguish_redacted_tags(capture)
    offline_store.execute(text("UPDATE printers SET model='N10' WHERE id=199"))
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex,do_not_feed,do_not_feed_note) "
                               "VALUES(199,7,'ABS','123456',true,'Hold')"))
    offline_store.commit()
    send(capture)
    def slot():
        return next(s for s in view()['filament_slots'] if s['slot_number'] == 7)
    first = slot()
    assert (first['filament_type'], first['color_hex'], first['do_not_feed_note']) == ('PLA', 'AE835B', 'Hold')
    tray = capture['ams']['ams'][0]['tray'][6]
    tray.update(tag_uid='00000000000000FE', tray_uuid='000000000000000000000000000000FE', tray_color='657483FF', remain=81)
    send({'ams': {'ams': [{'id': '32', 'tray': [tray]}]}}, 2061)
    second = slot()
    assert second['assigned_spool_id'] != first['assigned_spool_id'] and second['spool_confirmed']
    assert second['color_hex'] == '657483' and second['remaining'] == 81 and second['do_not_feed']
    hand = {'id': '249', 'tray_type': 'TPU', 'tray_info_idx': 'hand-fed', 'tray_color': 'AB1293FF'}
    send({'ams': {'ams': [{'id': '32', 'tray': [{'id': '6', 'state': 0}]}]}, 'vir_slot': [hand]}, 2122)
    empty = slot()
    assert (empty['filament_type'], empty['assigned_spool_id'], empty['remaining'], empty['color_hex']) == ('empty', None, None, None)
    assert empty['do_not_feed'] and empty['rack_filament']['type'] == 'TPU'
    # Either source can arrive alone, in either order.
    send({'ams': {'ams': [{'id': '32', 'tray': [tray]}]}}, 2183)
    restored = slot()
    assert restored['assigned_spool_id'] == second['assigned_spool_id']
    assert restored['rack_filament'] == empty['rack_filament']
    send({'vir_slot': [{'id': '249', 'tray_type': '', 'tray_info_idx': '', 'tray_color': '00000000'}]}, 2244)
    assert slot()['rack_filament'] is None and slot()['color_hex'] == '657483'


@pytest.mark.parametrize('trigger', [{'gcode_state': 'PAUSE'}, {'gcode_state': 'FAILED'}, {'print_error': 1}])
def test_unknown_hms_becomes_actionable_on_state_delta(inventory, offline_store, trigger):
    send, view, alerts = inventory
    offline_store.execute(text("UPDATE printers SET model='H3C',last_error_code='previous',last_error_message='Earlier event',"
                               "last_error_at='2020-01-01' WHERE id=199"))
    offline_store.commit()
    event = {'attr': 0x0300E400, 'code': 0x00020007}
    send({'hms': [event], 'print_error': 0, 'gcode_state': 'RUNNING'})
    shown = view()
    assert (shown['last_error_code'], shown['last_error_message']) == ('previous', 'Earlier event')
    assert shown['last_error_at'].startswith('2020-01-01')
    assert json.loads(shown['hms_errors'])[0]['severity'] == 'info' and not alerts
    send(trigger, 2011)
    shown = view()
    assert shown['last_error_code'] == '0300E400_00020007'
    assert json.loads(shown['hms_errors'])[0]['severity'] == 'error'
    assert [alert['alert_type'] for alert in alerts] == ['printer_error']
    history = view('/printers/199/hms-history')['entries']
    assert {entry['severity'] for entry in history} == {'info', 'error'}


def test_catalogued_p1s_fault_still_alerts(inventory, offline_store, monkeypatch):
    from modules.printers import bambu_hms_bundle
    send, view, alerts = inventory
    monkeypatch.setattr(bambu_hms_bundle, 'credential_prefix', lambda _: '01P')
    from modules.notifications import error_handling
    monkeypatch.setattr(error_handling, 'credential_prefix', bambu_hms_bundle.credential_prefix)
    offline_store.execute(text("UPDATE printers SET model='P1S' WHERE id=199"))
    offline_store.commit()
    send({'hms': [{'attr': 0x03003100, 'code': 0x00010001}], 'print_error': 0, 'gcode_state': 'RUNNING'})
    printer = view()
    event, = json.loads(printer['hms_errors'])
    assert event['severity'] == 'critical' and 'no description' not in event['message']
    assert printer['last_error_code'] == event['code'] and len(alerts) == 1


def test_get_version_applies_waiting_trays_without_a_second_ams_report(inventory, v2_offline_monitor, offline_store):
    from modules.printers.telemetry.bambu.raw import BambuInfoSection
    from modules.printers.telemetry.events import BambuInfoEvent
    send, view, _ = inventory
    _, emitters, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model=NULL,machine_type=NULL WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    distinguish_redacted_tags(capture)
    send(capture)
    assert all(slot['assigned_spool_id'] is None for slot in view()['filament_slots'])
    version = BambuInfoSection.model_validate({'command': 'get_version', 'module': [
        {'name': 'ota', 'sw_ver': '1', 'product_name': 'Bambu Lab H3C'}]})
    emitters[0](BambuInfoEvent('monitor-199', 2001, version))
    send({'gcode_state': 'IDLE'}, 2061)
    assert {slot['slot_number'] for slot in view()['filament_slots'] if slot['assigned_spool_id']} == {1, 7, 9}


def test_zone_delta_uses_reported_slots_at_the_next_sample(inventory, offline_store, token_client):
    from modules.printers import routes_ams_env
    send, view, _ = inventory
    token_client[0].app.include_router(routes_ams_env.router)
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    distinguish_redacted_tags(capture)
    send(capture)
    path = '/printers/199/ams/environment'
    assert {key: len(rows) for key, rows in view(path)['units'].items()} == {'0': 1, '1': 1}
    zone = dict(capture['ams']['ams'][0]['zone_info'][0], zone_slots=[0, 2], humidity_raw=42)
    send({'ams': {'ams': [{'id': '32', 'zone_info': [zone]}]}}, 2200)
    assert len(view(path)['units']['0']) == 1
    send({'gcode_state': 'IDLE'}, 2300)
    history = view(path)
    assert {key: len(rows) for key, rows in history['units'].items()} == {'0': 2, '1': 1}
    assert history['labels']['0'] == 'Drying slots 1, 3'
    assert any(row['humidity_percent'] == 42 for row in history['units']['0'])


def test_two_tray_changes_and_zone_delta_before_inventory_sync(inventory, offline_store):
    send, view, _ = inventory
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    distinguish_redacted_tags(capture)
    send(capture)
    def unit(delta, at):
        send({'ams': {'ams': [{'id': '32', **delta}]}}, at)
    unit({'tray': [{'id': '6', 'state': 0}]}, 2010)
    unit({'zone_info': capture['ams']['ams'][0]['zone_info']}, 2020)
    unit({'tray': [{'id': '8', 'state': 0}]}, 2030)
    send({'gcode_state': 'IDLE'}, 2060)
    assert [s['slot_number'] for s in view()['filament_slots'] if s['assigned_spool_id']] == [1]
