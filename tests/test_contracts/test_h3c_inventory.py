"""Captured AMS 3H reports through the monitor and authenticated customer routes."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import review_db, token_client
from tests.test_contracts.test_h3c_readonly import capture_packet
from tests.test_contracts.test_printer_offline_behavior import offline_store, v2_offline_monitor


@pytest.fixture
def inventory(v2_offline_monitor, offline_store, token_client, monkeypatch):
    from modules.notifications import error_handling
    from modules.printers import routes_crud, routes_status
    monitor, _, runtime = v2_offline_monitor
    from core import db
    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
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
    before = view()['filament_slots']
    send(capture)
    assert view()['filament_slots'] == before
    assert offline_store.execute(text('SELECT count(*) FROM ams_telemetry WHERE printer_id=199')).scalar_one() == 0


def test_tray_changes_and_hand_feed_use_separate_sources(inventory, offline_store):
    send, view, _ = inventory
    capture = capture_packet('ams3h-loaded')
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
    send(trigger, 2001)
    shown = view()
    assert shown['last_error_code'] == '0300E400_00020007'
    assert json.loads(shown['hms_errors'])[0]['severity'] == 'error'
    assert [alert['alert_type'] for alert in alerts] == ['printer_error']
    history = view('/printers/199/hms-history')['entries']
    code = shown['last_error_code']
    assert [(e['code'], e['severity']) for e in sorted(history, key=lambda e: e['id'])] == [(code, 'info'), (code, 'error')]
    from datetime import datetime, timedelta
    from modules.reporting.failure_analytics import build_failure_analytics
    analytics = build_failure_analytics(offline_store, {'role': 'admin', 'group_id': None}, datetime.now() - timedelta(days=1))
    assert [(e['code'], e['count']) for e in analytics['hms_errors']] == [(code, 1)]


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


@pytest.mark.parametrize('zone_delta', [False, True])
def test_get_version_applies_waiting_trays_without_a_second_ams_report(inventory, v2_offline_monitor, offline_store, zone_delta):
    from modules.printers.telemetry.bambu.raw import BambuInfoSection
    from modules.printers.telemetry.events import BambuInfoEvent
    send, view, _ = inventory
    _, emitters, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model=NULL,machine_type=NULL WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    send(capture)
    assert all(slot['assigned_spool_id'] is None for slot in view()['filament_slots'])
    if zone_delta:
        zone = dict(capture['ams']['ams'][0]['zone_info'][0], humidity_raw=42)
        send({'ams': {'ams': [{'id': '32', 'zone_info': [zone]}]}}, 2001)
    version = BambuInfoSection.model_validate({'command': 'get_version', 'module': [
        {'name': 'ota', 'sw_ver': '1', 'product_name': 'Bambu Lab H3C'}]})
    emitters[0](BambuInfoEvent('monitor-199', 2001, version))
    send({'gcode_state': 'IDLE'}, 2061)
    assert {slot['slot_number'] for slot in view()['filament_slots'] if slot['assigned_spool_id']} == {1, 7, 9}
    send({'gcode_state': 'IDLE'}, 2300)
    expected = {z['zone_id']: int(z['humidity_raw']) for z in capture['ams']['ams'][0]['zone_info']}
    if zone_delta:
        expected[0] = 42
    assert dict(offline_store.execute(text('SELECT ams_unit,humidity_percent FROM ams_telemetry WHERE printer_id=199')).all()) == expected


def test_reconnect_just_after_get_version_waits_for_new_rack_roster(inventory, v2_offline_monitor):
    from modules.printers.telemetry.bambu.raw import BambuInfoSection
    from modules.printers.telemetry.events import BambuInfoEvent
    send, view, _ = inventory
    monitor, emitters, _ = v2_offline_monitor
    capture = capture_packet('ams3h-loaded')
    send(capture)
    version = BambuInfoSection.model_validate({'command': 'get_version', 'module': [
        {'name': 'ota', 'sw_ver': '1', 'product_name': 'Bambu Lab H3C'}]})
    emitters[0](BambuInfoEvent('monitor-199', 2001, version))
    before = view()['filament_slots']
    monitor._begin_connection()
    send({'ams': {'tray_exist_bits': capture['ams']['tray_exist_bits']}}, 2061)
    assert view()['filament_slots'] == before
    send(capture, 2122)
    assert {s['slot_number'] for s in view()['filament_slots'] if s['assigned_spool_id']} == {1, 7, 9}


def test_zone_delta_uses_reported_slots_at_the_next_sample(inventory, offline_store, token_client):
    from modules.printers import routes_ams_env
    send, view, _ = inventory
    token_client[0].app.include_router(routes_ams_env.router)
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    send(capture)
    path = '/printers/199/ams/environment'
    assert {key: len(rows) for key, rows in view(path)['units'].items()} == {'0': 1, '1': 1}
    zone = dict(capture['ams']['ams'][0]['zone_info'][0], zone_slots=[0, 2], humidity_raw=42)
    send({'ams': {'ams': [{'id': '32', 'zone_info': [zone]}]}}, 2200)
    assert len(view(path)['units']['0']) == 1
    send({'gcode_state': 'IDLE'}, 2300)
    history = view(path)
    assert {key: len(rows) for key, rows in history['units'].items()} == {'0': 2, '1': 2}
    assert history['labels']['0'] == 'Slots 1, 3'
    assert any(row['humidity_percent'] == 42 for row in history['units']['0'])
    send({'gcode_state': 'IDLE'}, 2600)
    assert {key: len(rows) for key, rows in view(path)['units'].items()} == {'0': 3, '1': 3}


def test_two_tray_changes_and_zone_delta_before_inventory_sync(inventory, offline_store):
    send, view, _ = inventory
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    send(capture)
    def unit(delta, at):
        send({'ams': {'ams': [{'id': '32', **delta}]}}, at)
    unit({'tray': [{'id': '6', 'state': 0}]}, 2010)
    unit({'zone_info': capture['ams']['ams'][0]['zone_info']}, 2020)
    unit({'tray': [{'id': '8', 'state': 0}]}, 2030)
    send({'gcode_state': 'IDLE'}, 2060)
    assert [s['slot_number'] for s in view()['filament_slots'] if s['assigned_spool_id']] == [1]


def test_remain_only_tray_delta_keeps_filament_and_binding(inventory, offline_store):
    send, view, _ = inventory
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    send(capture_packet('ams3h-loaded'))
    first = view()['filament_slots'][6]
    send({'ams': {'ams': [{'id': '32', 'tray': [{'id': '6', 'remain': 80}]}]}}, 2061)
    updated = view()['filament_slots'][6]
    assert updated['remaining'] == 80
    for key in ('filament_type', 'color_hex', 'assigned_spool_id', 'preset_id'):
        assert updated[key] == first[key] and updated[key] is not None


@pytest.mark.parametrize('model,unit,slot,bits', [('P1S', '0', 1, '1'), ('H2D', '128', 17, '10000')])
def test_bit_only_after_reconnect_preserves_slots(inventory, v2_offline_monitor, offline_store, model, unit, slot, bits):
    from sqlalchemy import event
    send, view, _ = inventory
    # The base monitor refreshes loaded_at on each committed sync, even if the
    # bits are unchanged. Pin the database clock to compare the entire slot row.
    @event.listens_for(offline_store.get_bind(), 'checkout')
    def pin_time(connection, *_):
        connection.create_function('current_timestamp', 0, lambda: '2026-11-21 12:00:00')

    offline_store.execute(text("UPDATE printers SET model=:model WHERE id=199"), {'model': model})
    offline_store.commit()
    tray = dict(capture_packet('ams3h-loaded')['ams']['ams'][0]['tray'][0], id='0')
    send({'ams': {'ams': [{'id': unit, 'tray': [tray]}], 'tray_exist_bits': bits}})
    before = view()['filament_slots']
    assert any(s['slot_number'] == slot and s['assigned_spool_id'] for s in before)
    v2_offline_monitor[0]._begin_connection()
    send({'ams': {'tray_exist_bits': bits}}, 2061)
    assert view()['filament_slots'] == before


@pytest.mark.parametrize('prefix', ['01P', 'ZZZ'])
def test_undescribed_level_one_alerts_with_or_without_bundle(inventory, monkeypatch, prefix):
    from modules.notifications import error_handling
    from modules.printers import bambu_hms_bundle
    monkeypatch.setattr(error_handling, 'credential_prefix', lambda _: prefix)
    monkeypatch.setattr(bambu_hms_bundle, 'credential_prefix', lambda _: prefix)
    send, view, alerts = inventory
    send({'hms': [{'attr': 0x0300E400, 'code': 0x00010007}], 'print_error': 0, 'gcode_state': 'RUNNING'})
    shown = view()
    assert shown['last_error_code'] == '0300E400_00010007'
    assert json.loads(shown['hms_errors'])[0]['severity'] == 'critical'
    assert [alert['alert_type'] for alert in alerts] == ['printer_error']


def test_hms_notices_return_separately_and_parse_failure_retries(inventory, monkeypatch):
    from modules.notifications import error_handling
    raw = [{'attr': 0x0300E400, 'code': 0x00020007}]
    result = error_handling.process_hms_errors(199, raw, quiet_unknown=True)
    assert result['errors'] == []
    assert [e['code'] for e in result['notices']] == ['0300E400_00020007']
    critical = {'attr': 0x0300E400, 'code': 0x00010007}
    mixed = error_handling.process_hms_errors(199, [*raw, critical], quiet_unknown=True)
    assert [e['code'] for e in mixed['errors']] == ['0300E400_00010007']
    assert mixed['notices'] == result['notices']
    assert error_handling.process_hms_errors(199, []) == {'errors': []}
    parse = error_handling.parse_hms_errors
    def fail_current(data, *args, **kwargs):
        if data is raw:
            raise ValueError('catalog unavailable')
        return parse(data, *args, **kwargs)
    monkeypatch.setattr(error_handling, 'parse_hms_errors', fail_current)
    assert error_handling.process_hms_errors(199, raw) == {'retryable': True}


def test_mixed_hms_history_counts_each_activation_once(inventory, monkeypatch):
    from modules.printers import bambu_hms_bundle
    from modules.notifications import error_handling
    for module in (bambu_hms_bundle, error_handling):
        monkeypatch.setattr(module, 'credential_prefix', lambda _: '01P')
    send, view, _ = inventory
    events = [{'attr': 0x0300E400, 'code': 0x00020007}, {'attr': 0x03003100, 'code': 0x00010001}]
    send({'hms': events, 'print_error': 0, 'gcode_state': 'RUNNING'})
    send({'gcode_state': 'PAUSE'}, 2001)
    send({'gcode_state': 'RUNNING'}, 2002)
    send({'gcode_state': 'PAUSE'}, 2061)
    send({'gcode_state': 'RUNNING'}, 2122)
    history = view('/printers/199/hms-history')['entries']
    assert sorted((e['code'], e['severity']) for e in history) == [
        ('03003100_00010001', 'critical'), ('0300E400_00020007', 'error'), ('0300E400_00020007', 'info')]


def test_status_without_identification_uses_generic_ams(inventory, v2_offline_monitor, monkeypatch):
    from modules.printers.monitors import nozzle_observations
    send, view, _ = inventory
    monitor = v2_offline_monitor[0]
    monkeypatch.delattr(monitor, '_identified_h3c', raising=False)
    monkeypatch.setattr(nozzle_observations, 'observe', lambda *a: None)
    send({'ams': {'ams': [{'id': '0', 'tray': [{'id': '0', 'tray_type': 'PLA'}]}]}})
    assert view()['filament_slots'][0]['filament_type'] == 'PLA'


def test_history_parser_failure_is_logged_and_retried(inventory, monkeypatch, caplog):
    import logging
    from modules.notifications import event_dispatcher
    send, view, _ = inventory
    parse = event_dispatcher.parse_hms_errors
    def fail(*args, **kwargs):
        raise ValueError('test catalog unavailable')
    monkeypatch.setattr(event_dispatcher, 'parse_hms_errors', fail)
    with caplog.at_level(logging.DEBUG, logger='mqtt_monitor'):
        send({'hms': [{'attr': 0x0300E400, 'code': 0x00020007}], 'gcode_state': 'RUNNING', 'print_error': 0})
    assert any('HMS history insert' in r.message for r in caplog.records)
    assert view('/printers/199/hms-history')['entries'] == []
    monkeypatch.setattr(event_dispatcher, 'parse_hms_errors', parse)
    send({'gcode_state': 'RUNNING'}, 2061)
    assert len(view('/printers/199/hms-history')['entries']) == 1


def test_hms_reactivation_between_heartbeats(inventory):
    send, view, _ = inventory
    event = {'attr': 0x0300E400, 'code': 0x00020007}
    send({'hms': [event], 'print_error': 0, 'gcode_state': 'RUNNING'})
    send({'hms': []}, 2001)
    send({'hms': [event]}, 2002)
    send({'hms': []}, 2003)
    assert [e['source'] for e in view('/printers/199/hms-history')['entries']] == ['bambu_notice'] * 2


def test_zone_expiry_and_resumption_are_per_zone(inventory, offline_store, token_client):
    from modules.printers import routes_ams_env
    send, view, _ = inventory
    token_client[0].app.include_router(routes_ams_env.router)
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    capture = capture_packet('ams3h-loaded')
    send(capture)
    zones = capture['ams']['ams'][0]['zone_info']
    def counts():
        return {k: len(v) for k, v in view('/printers/199/ams/environment')['units'].items()}

    send({'ams': {'ams': [{'id': '32', 'zone_info': [zones[0]]}]}}, 2300)
    send({'gcode_state': 'IDLE'}, 2600)
    assert counts() == {'0': 3, '1': 3}
    send({'gcode_state': 'IDLE'}, 2900)
    assert counts() == {'0': 4, '1': 3}
    send({'gcode_state': 'IDLE'}, 3200)
    assert counts() == {'0': 4, '1': 3}
    send({'ams': {'ams': [{'id': '32', 'zone_info': [zones[1]]}]}}, 3500)
    assert counts() == {'0': 4, '1': 4}


@pytest.mark.parametrize('model', ['P1S', 'H2D'])
def test_reconnect_bit_removal_matches_base(inventory, v2_offline_monitor, offline_store, model):
    send, view, _ = inventory
    offline_store.execute(text('UPDATE printers SET model=:model WHERE id=199'), {'model': model})
    offline_store.commit()
    from hashlib import sha256
    def snapshot():
        slots = view()['filament_slots']
        rows = [(s['slot_number'], s['filament_type'], s['color_hex'], s['assigned_spool_id'], s['spool_confirmed'])
                for s in slots]
        normalized = [dict(s, loaded_at=bool(s['loaded_at']), observed_state=bool(s['observed_state'])) for s in slots]
        return rows, sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()

    tray = {'id': '0', 'tray_type': 'PLA', 'tray_color': 'ABCDEFff', 'tag_uid': 'A001'}
    units = [{'id': '0', 'tray': [tray]}, {'id': '128', 'tray': [dict(tray, tag_uid='A017')]}]
    full = {'ams': {'ams': units, 'tray_exist_bits': '10001'}}
    send(full)
    initial, initial_hash = snapshot()
    v2_offline_monitor[0]._begin_connection()
    send({'ams': {'tray_exist_bits': '1'}}, 2061)
    removed, removed_hash = snapshot()
    send(full, 2122)
    restored, restored_hash = snapshot()
    # Observed through GET /printers/199 on 75e4e109 for both printer models.
    standard = (1, 'PLA', 'ABCDEF', 1, True)
    ht = (17, 'PLA', 'ABCDEF', 2, True)
    assert initial == restored == [standard, ht]
    assert removed == [standard, (17, 'empty', None, None, False)]
    assert initial_hash == restored_hash == '1839d43be0826a03e8b548a6a3bad8f06ec7973a2d252e840ab3bdb2c1b08860'
    assert removed_hash == 'b6884d29b28ae89ad41eb2e070d802c0014b76406654361be6e30987cb65493c'


@pytest.mark.parametrize('model', ['P1S', 'H2D'])
def test_first_bits_without_a_roster_matches_base(inventory, offline_store, model):
    send, view, _ = inventory
    offline_store.execute(text('UPDATE printers SET model=:model WHERE id=199'), {'model': model})
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                               "VALUES(199,17,'PETG','123456')"))
    offline_store.commit()
    send({'ams': {'tray_exist_bits': '0'}})
    ht, = view()['filament_slots']
    assert (ht['slot_number'], ht['filament_type'], ht['color_hex'], ht['assigned_spool_id']) == (17, 'empty', None, None)


@pytest.mark.parametrize('field,value', [('tray_color', '12AB34FF'), ('tray_type', 'PETG'), ('tray_info_idx', 'GFA99')])
def test_tagless_tray_change_releases_old_spool(inventory, offline_store, field, value):
    send, view, _ = inventory
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    send(capture_packet('ams3h-loaded'))
    assert view()['filament_slots'][6]['assigned_spool_id'] is not None
    send({'ams': {'ams': [{'id': '32', 'tray': [{'id': '6', field: value}]}]}}, 2061)
    slot = view()['filament_slots'][6]
    assert slot['assigned_spool_id'] is None
    assert slot['filament_type'] == ('PETG' if field == 'tray_type' else 'PLA')
