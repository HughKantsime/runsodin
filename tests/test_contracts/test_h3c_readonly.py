"""Read-only H3C support against the redacted idle report and local transports."""
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import Depends
from sqlalchemy import text

from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_api_token_delegation_behavior import review_db, token_client
from tests.test_contracts.test_printer_offline_behavior import offline_store, v2_offline_monitor


STORED_RACK_ROWS = json.dumps([{'side': 'rack', 'position': n, 'nozzle_id': 15 + n} for n in (1, 2, 3)])


def capture_packet(name):
    path = Path(__file__).parents[1] / f'fixtures/telemetry/h3c-{name}-2026-11-21.json'
    return json.loads(path.read_text())['message']['print']


@pytest.fixture
def capture(request, monkeypatch, review_db):
    from core import db
    # The full packet also wakes the monitor's HMS worker. Keep it on the owned database.
    monkeypatch.setattr(db, 'engine', review_db.get_bind())
    return capture_packet(getattr(request, 'param', 'idle'))


def test_capture_through_monitor_and_printer_views(capture, v2_offline_monitor, offline_store, token_client, monkeypatch):
    from modules.printers import routes_crud, routes_status
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.printers.telemetry.events import BambuReportEvent
    from modules.printers.services import bambu_tray_counts

    monitor, emit, runtime = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model='Bambu Lab H3C',tags=:tags,slot_count=9,installed_nozzles=:old WHERE id=199"),
                          {'tags': json.dumps(['rack']), 'old': json.dumps([{'extruder': 0, 'side': 'single', 'present': False}])})
    for n in range(1, 10):
        offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color) "
                                   "VALUES(199,:n,'empty',:color)"), {'n': n, 'color': 'Blue' if n == 2 else None})
    offline_store.commit()
    assert bambu_tray_counts(capture) == {'ams_slots': 0, 'ams_ht_slots': 0}
    emit[0](BambuReportEvent('monitor-199', 2000, BambuPrintSection.model_validate(capture), False))
    assert 'ams_connected' not in monitor._display_facts
    def stored():
        return json.loads(offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one())
    rows = stored()
    assert [(r['nozzle_id'], r['position'], r['type'], r['diameter'], r['present'], r['stat']) for r in rows] == [
        (16 + i, i + 1, 'HT01' if i == 7 else 'HS01', 0.4, True, 161 if i == 0 else 129) for i in range(9)]
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(time=lambda: 2011))
    monitor._on_status(SimpleNamespace(raw_data={'print': {'nozzle_type': 'HS01-0.4'}}))
    assert stored() == rows

    client, issue = token_client
    client.app.include_router(routes_crud.router)
    client.app.include_router(routes_status.router)
    headers = issue(['read:printers'])
    public = [{k: v for k, v in row.items() if k != 'stat'} for row in rows]
    listed = client.get('/printers', params={'tag': 'rack'}, headers=headers).json()
    detail = client.get('/printers/199', headers=headers).json()
    for printer in [next(p for p in listed if p['id'] == 199), detail]:
        assert printer['model'] == 'H3C' and printer['installed_nozzles'] == public
        assert 'ams_connected' not in printer and printer['loaded_colors'] == ['blue']
        assert sorted(slot['slot_number'] for slot in printer['filament_slots']) == list(range(1, 10))
        assert all(slot['rack_filament'] is None and slot['observed_state'] is None and slot['filament_type'] == 'empty'
                   for slot in printer['filament_slots'])
    status = client.get('/printers/199/nozzle-status', headers=headers).json()
    assert status['installed_nozzles'] == public and status['nozzle_count'] == 1
    assert offline_store.execute(text('SELECT slot_number,color FROM filament_slots WHERE printer_id=199 ORDER BY slot_number')).all() == [
        (n, 'Blue' if n == 2 else None) for n in range(1, 10)]


@pytest.mark.parametrize('model', ['P1S', 'H3C'])
@pytest.mark.parametrize('path', ['/printers', '/printers/199'])
def test_printer_views_skip_malformed_nozzle_rows(model, path, offline_store, token_client):
    from modules.printers import routes_crud
    offline_store.execute(text('UPDATE printers SET model=:model,installed_nozzles=:rows,tags=:tags WHERE id=199'),
                          {'model': model, 'rows': '[null, {}, 7]', 'tags': '["nozzle-test"]'})
    offline_store.commit()
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    params = {'tag': 'nozzle-test'} if path == '/printers' else {}
    response = client.get(path, params=params, headers=issue(['read:printers']))
    assert response.status_code == 200
    data = response.json()
    printer = next(row for row in data if row['id'] == 199) if path == '/printers' else data
    assert printer['installed_nozzles'] == []


@pytest.mark.parametrize('capture', ['idle', 'ams3h-loaded'], indirect=True)
@pytest.mark.parametrize('transport', ['v2', 'v2-reconnect', 'legacy'])
def test_rack_report_before_model_identification(capture, v2_offline_monitor, offline_store, monkeypatch, transport):
    from modules.printers.telemetry.bambu.raw import BambuInfoSection
    from modules.printers.telemetry.events import BambuInfoEvent
    monitor, emit, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET model='P1S' WHERE id=199"))
    offline_store.commit()
    info = {'command': 'get_version', 'module': [
        {'name': 'ota', 'sw_ver': '1', 'product_name': 'Bambu Lab H3C'}]}
    if transport == 'legacy':
        from modules.printers.adapters.bambu import BambuPrinter
        from modules.printers.telemetry import feature_flag
        monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: False)
        monkeypatch.setattr(BambuPrinter, 'connect', lambda self: True)
        assert monitor.connect()
        monitor._bambu._parse_status({'print': capture})
        monitor._bambu._parse_status({'info': info})
    else:
        monitor._on_status(SimpleNamespace(raw_data={'print': capture}))
        if transport == 'v2-reconnect':
            from modules.printers.telemetry.events import ConnectionEvent
            emit[0](ConnectionEvent('monitor-199', 2001, 'connected'))
        emit[0](BambuInfoEvent('monitor-199', 2001, BambuInfoSection.model_validate(info)))
    model, stored = offline_store.execute(text('SELECT machine_type,installed_nozzles FROM printers WHERE id=199')).one()
    assert model == 'H3C'
    observations = offline_store.execute(text('SELECT tray_observation FROM filament_slots WHERE printer_id=199')).scalars()
    readings = [json.loads(raw)['rack_filament'] for raw in observations]
    assert {r['id'] for r in readings} == set(range(247, 256))
    if capture['ams']['ams']:
        assert {r['id']: r['preset'] for r in readings if r['type']} == {255: 'GFA01', 249: 'GFA01', 247: 'GFA00'}
    assert [row['position'] for row in json.loads(stored)] == list(range(1, 10))


def emit_rack_report(monitor, store, capture):
    store.execute(text("UPDATE printers SET machine_type='Bambu Lab H3C' WHERE id=199"))
    store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print': capture}))


@pytest.mark.parametrize('capture,loaded', [('idle', False), ('one-filament', True)], indirect=['capture'])
def test_reported_filaments_by_raw_id(capture, loaded, offline_store, v2_offline_monitor):
    monitor, _, _ = v2_offline_monitor
    emit_rack_report(monitor, offline_store, capture)

    def snapshot():
        count = offline_store.execute(text('SELECT slot_count FROM printers WHERE id=199')).scalar_one()
        rows = offline_store.execute(text('SELECT slot_number,tray_observation FROM filament_slots WHERE printer_id=199'))
        return count, {position: json.loads(raw)['rack_filament'] for position, raw in rows}

    count, readings = snapshot()
    assert count == 9
    assert {position: reading['id'] for position, reading in readings.items()} == dict(enumerate(range(255, 246, -1), 1))
    assert all(reading['observed_at'] for reading in readings.values())
    expected = ('PLA', 'GFA01', 'AE96D4FF') if loaded else ('', '', '00000000')
    assert tuple(readings[1][field] for field in ('type', 'preset', 'color')) == expected
    assert all(readings[n]['type'] == '' for n in range(2, 10))
    slot_values = offline_store.execute(text('SELECT filament_type,color_hex FROM filament_slots '
                                            'WHERE printer_id=199 ORDER BY slot_number')).all()
    assert slot_values == [('empty', None)] * 9
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'nozzle_type': 'HS01-0.4'}}))
    assert snapshot() == (count, readings)


@pytest.mark.parametrize('capture', ['one-filament'], indirect=True)
def test_rack_slot_observation_preserves_operator_edit(capture, offline_store, v2_offline_monitor, token_client):
    from modules.inventory.routes import filament_slots
    from modules.printers import routes_crud
    from tests.test_contracts.test_spool_rfid_identity import seed

    monitor, _, _ = v2_offline_monitor
    emit_rack_report(monitor, offline_store, capture)
    offline_store.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(199,'Test','Purple','PETG')"))
    offline_store.commit()
    seed(offline_store.get_bind(), filament=199, sealed=False)
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    client.app.include_router(filament_slots.router)
    headers = issue(['admin', 'write:printers', 'read:printers'])
    edit = client.patch('/printers/199/slots/1', headers=headers, json={
        'color_hex': '112233', 'filament_type': 'PETG', 'assigned_spool_id': 56})
    assert edit.status_code == 200
    def slot():
        return client.get('/printers/199', headers=headers).json()['filament_slots'][0]
    before = slot()
    assert (before['color_hex'], before['filament_type'], before['assigned_spool_id']) == ('112233', 'PETG', 56)
    def report(packet, **kwargs):
        monitor._last_heartbeat = 0
        monitor._on_status(SimpleNamespace(raw_data={'print': packet}, **kwargs))
        current = slot()
        assert {key: current[key] for key in ('color_hex', 'filament_type', 'assigned_spool_id', 'loaded_at')} == {
            key: before[key] for key in ('color_hex', 'filament_type', 'assigned_spool_id', 'loaded_at')}
        assert not current['spool_change_pending'] and current['observed_state'] is None
        return current['rack_filament']

    assert report(capture)['color'] == 'AE96D4FF'
    reading = report({'vir_slot': [{'id': '255', 'tray_type': 'ABS'}]})
    assert (reading['type'], reading['preset'], reading['color']) == ('ABS', 'GFA01', 'AE96D4FF')
    idle = json.loads((Path(__file__).parents[1] / 'fixtures/telemetry/h3c-idle-2026-11-21.json').read_text())
    assert report(idle['message']['print'], mqtt_retained=True) == reading
    assert report(idle['message']['print'], report_age_seconds=61) == reading
    # An out-of-order packet within the freshness window, coalesced with a newer
    # delta on the next heartbeat, cannot lend its older fields the newer time.
    monitor._on_status(SimpleNamespace(raw_data={'print': idle['message']['print']}, report_age_seconds=2))
    cleared = report({'vir_slot': [{'id': '255', 'tray_info_idx': ''}]})
    assert (cleared['type'], cleared['preset'], cleared['color']) == ('ABS', '', 'AE96D4FF')
    resize = client.patch('/printers/199', headers=headers, json={'slot_count': 1})
    assert resize.status_code == 422


def test_presence_uses_nozzle_id_and_position_uses_address(capture):
    from modules.printers.nozzle_capability import installed_nozzles
    rack = capture['device']['nozzle']
    rack['exist'] ^= 1 << 23
    rack['info'][7]['addr_id'], rack['info'][1]['addr_id'] = 1, 7
    rows = installed_nozzles(capture, include_rack=True)
    entry = next(row for row in rows if row.get('nozzle_id') == 23)
    assert (entry['position'], entry['type'], entry['present']) == (2, 'HT01', False)
    assert entry['virtual_slot_id'] == 254


@pytest.mark.parametrize('model,machine,file_model', [
    ('N10', None, 'Bambu Lab P1S'), (None, None, 'Bambu Lab P1S'),
    ('H3C', 'P1S', 'Bambu Lab P1S'), ('P1S', 'Bambu Lab H3C', 'Bambu Lab H3C'), ('H3C', None, None)])
def test_dispatch_refuses_target_before_device_or_file_inspection(review_db, tmp_path, monkeypatch, model, machine, file_model):
    from modules.printers import dispatch, smart_plug
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu')
    if file_model:
        with ZipFile(path, 'a') as package:
            package.writestr('Metadata/project_settings.config', json.dumps({'printer_model': file_model}))
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET model=:model,machine_type=:machine,installed_nozzles=:rows WHERE id=59"),
                     {'model': model, 'machine': machine, 'rows': STORED_RACK_ROWS if model is None else '[]'})
    monkeypatch.setattr(dispatch, '_validated_print_options', lambda *a: pytest.fail('Reached file inspection'))
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', lambda *a: pytest.fail('Reached device preparation'))
    ok, reason = dispatch.dispatch_job(59, 53)
    assert not ok and reason.startswith('unsupported_rack_printer:')
    assert 'rack dispatch is pending live certification' in reason.lower()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0


def test_dispatch_to_p1s_ignores_stored_rack_rows(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from tests.test_contracts.test_multi_source_ams_dispatch import transport
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu')
    with ZipFile(path, 'a') as package:
        package.writestr('Metadata/project_settings.config', json.dumps({'printer_model': 'Bambu Lab P1S'}))
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET model='P1S',machine_type=NULL,installed_nozzles=:rows WHERE id=59"),
                     {'rows': STORED_RACK_ROWS})
    transport(monkeypatch, path, 'v2', [])
    _, reason = dispatch.dispatch_job(59, 53)
    assert not reason.startswith('unsupported_rack_printer:')


def test_printer_controls_stay_read_only(offline_store, token_client):
    from modules.printers import routes_controls, routes_experimental, routes_ams_env, services
    offline_store.execute(text("UPDATE printers SET machine_type='Bambu Lab H3C' WHERE id=199"))
    offline_store.commit()
    client, issue = token_client
    client.app.include_router(routes_controls.router, dependencies=[Depends(routes_experimental.experimental_controls_guard)])
    client.app.include_router(routes_ams_env.router)
    headers = issue(['admin', 'write:printers'])
    for action in ('lights', 'pause', 'resume', 'stop', 'ams/refresh'):
        response = client.post(f'/printers/199/{action}', headers=headers)
        assert response.status_code == 409 and response.json()['detail']['code'] == 'printer_type_monitoring_only'
    response = client.put('/printers/199/ams/0/slots/0', headers=headers, json={
        'preset_id': 'PLA', 'material': 'PLA', 'color': 'FFFFFF', 'nozzle_temp_min': 190, 'nozzle_temp_max': 220})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'printer_type_monitoring_only'
    printer = SimpleNamespace(api_type='bambu', model='H3C')
    assert services.request_observed_task_resume(printer, 199) == 'refused'


def test_h3c_create_and_setup_have_nine_slots(review_db):
    from modules.printers.schemas import PrinterCreate
    from modules.printers.services import create_printer_record
    from modules.system.routes_setup import SetupPrinterRequest
    fields = dict(name='Rack', api_type='bambu', model='Bambu Lab H3C')
    for request in (PrinterCreate, SetupPrinterRequest):
        assert request(**fields).slot_count == 9
        assert request(**fields, slot_count=9).slot_count == 9
    assert create_printer_record(review_db, **fields, slot_count=9).slot_count == 9


def test_rack_tool_change_without_nozzle_list(capture, offline_store, v2_offline_monitor):
    monitor, _, _ = v2_offline_monitor
    capture['device']['nozzle'].pop('src_id', None)
    emit_rack_report(monitor, offline_store, capture)
    def mounted():
        raw = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
        return [row['position'] for row in json.loads(raw) if row.get('mounted')]
    assert mounted() == []
    for source, expected in [(16, [1]), (20, [5]), (255, [])]:
        monitor._last_heartbeat = 0
        monitor._on_status(SimpleNamespace(raw_data={'print': {'device': {'nozzle': {'src_id': source}}}}))
        assert mounted() == expected


def test_nozzle_comparison_with_reported_rack(capture):
    from modules.printers.nozzle_capability import installed_nozzles, compare_nozzles
    rows = [row for row in installed_nozzles(capture, include_rack=True) if row['side'] == 'rack']
    assert compare_nozzles([{'extruder': 0, 'side': 'single'}], rows) == (None, True)


def test_h3c_empty_ams_report_preserves_operator_slot_above_nine(capture, offline_store, v2_offline_monitor):
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                               "VALUES(199,17,'PETG','112233')"))
    offline_store.commit()
    monitor, _, _ = v2_offline_monitor
    emit_rack_report(monitor, offline_store, capture)
    assert offline_store.execute(text("SELECT filament_type,color_hex FROM filament_slots WHERE "
                                      "printer_id=199 AND slot_number=17")).one() == ('PETG', '112233')


def test_h3c_release_note_describes_observations():
    changelog = (Path(__file__).parents[2] / 'CHANGELOG.md').read_text()
    entry = next(line.lower() for line in changelog.splitlines() if 'select the Bambu Lab H3C' in line)
    assert 'no ams is connected' not in entry
    for fact in ('nine rack positions', 'nozzle types', 'tool in the head', 'filament', 'dispatch', 'controls'):
        assert fact in entry


def test_identified_rack_does_not_show_an_old_ams_confirmation(offline_store, token_client):
    from modules.printers import routes_crud
    offline_store.execute(text("UPDATE printers SET model='P1S',machine_type='H3C' WHERE id=199"))
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,tray_observation) "
                               "VALUES(199,1,'PETG','Blue',:old)"), {'old': json.dumps({'id': 'a' * 32, 'pending': True})})
    offline_store.commit()
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    slot = client.get('/printers/199/slots', headers=issue(['read:printers'])).json()[0]
    assert (slot['spool_change_pending'], slot['observed_state'], slot['color']) == (False, None, 'Blue')


@pytest.mark.parametrize('sender', ['monitor', 'v2', 'legacy', 'fallback'])
@pytest.mark.parametrize('identity', ['model', 'get_version', 'N10', 'rack_report', 'mounted_report', 'rack_report_on_p1s'])
@pytest.mark.parametrize('api_type', ['bambu', None], ids=['bambu', 'unset'])
def test_rack_refusal_at_sender(sender, identity, api_type, capture, offline_store, v2_offline_monitor, monkeypatch):
    import time
    from unittest.mock import Mock
    from modules.printers import monitor_commands, route_utils
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.models import Printer
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    from modules.printers.telemetry.bambu.raw import BambuInfoSection
    from modules.printers.telemetry.events import BambuInfoEvent

    monitor, emit, _ = v2_offline_monitor
    wire = Mock()
    wire.publish.return_value.rc = 0
    wire.publish.return_value.is_published.return_value = True
    if sender in ('legacy', 'v2'):
        if sender == 'v2':
            adapter = BambuCommandAdapter(BambuAdapterConfig('199', monitor.serial, monitor.ip, ''))
        else:
            adapter = BambuPrinter(monitor.ip, monitor.serial, '')
        adapter._client = wire
        adapter._connected = True
        assert adapter.pause_print() is True
        wire.publish.assert_called_once()
        wire.publish.reset_mock()
    offline_store.execute(text("UPDATE printers SET model=:model,machine_type=NULL,api_type=:api,api_host=:host,api_key=:credential WHERE id=199"),
                          {'model': {'model': 'H3C', 'N10': 'N10', 'rack_report': None, 'mounted_report': None}.get(identity, 'P1S'),
                           'api': api_type, 'host': monitor.ip, 'credential': monitor.serial + '|fixture'})
    offline_store.commit()
    # A legacy fallback can hold the row it loaded before the monitor commits.
    target = offline_store.get(Printer, 199) if api_type is None else None
    monitor._last_heartbeat = 2000
    if sender == 'monitor':
        monitor._v2_adapter._client = wire
        monitor._v2_adapter._connected = True
    elif sender == 'fallback':
        def connect(adapter):
            adapter._client, adapter._connected = wire, True
            return True
        monkeypatch.setenv('ODIN_TELEMETRY_V2', '0')
        monkeypatch.setattr(BambuPrinter, 'connect', connect)

    def check_send(allowed):
        if sender == 'monitor':
            outcome = monitor_commands.execute({199: monitor}, dict(printer_id=199, action='light', on=True,
                                               deadline=time.monotonic() + 3))
            assert outcome == ('accepted' if allowed else 'refused')
        elif sender == 'fallback':
            assert route_utils._send_printer_command(target or offline_store.get(Printer, 199), 'pause_print') is allowed
        else:
            assert adapter.pause_print() is allowed

    def stored_nozzles():
        raw = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
        return json.loads(raw or '[]')

    if identity == 'mounted_report':
        capture = capture_packet('ams3h-loaded')
    if identity in ('rack_report', 'mounted_report', 'rack_report_on_p1s'):
        monitor._on_status(SimpleNamespace(raw_data={'print': {'device': capture['device']}}))
    if identity == 'rack_report_on_p1s':
        assert offline_store.execute(text('SELECT model,machine_type FROM printers WHERE id=199')).one() == ('P1S', None)
        assert all(row['side'] != 'rack' for row in stored_nozzles())
        check_send(allowed=True)
        topic = f'device/{monitor.serial}/request'
        if sender == 'monitor':
            payload = {'system': {'sequence_id': '0', 'command': 'ledctrl', 'led_node': 'chamber_light', 'led_mode': 'on'}}
            wire.publish.assert_called_once_with(topic, json.dumps(payload), qos=0)
        else:
            wire.publish.assert_called_once_with(topic, json.dumps({'print': {'sequence_id': '0', 'command': 'pause'}}))
        wire.publish.reset_mock()
    if identity in ('get_version', 'rack_report_on_p1s'):
        info = {'command': 'get_version', 'module': [{'name': 'ota', 'sw_ver': '1', 'product_name': 'Bambu Lab H3C'}]}
        emit[0](BambuInfoEvent('monitor-199', 2000, BambuInfoSection.model_validate(info)))
    if identity in ('rack_report', 'mounted_report', 'rack_report_on_p1s'):
        assert [row['position'] for row in stored_nozzles()] == list(range(1, 10))
    if sender == 'fallback':
        monkeypatch.setattr(route_utils, '_bambu_command', lambda *args: pytest.fail('Reached Bambu fallback'))
    check_send(allowed=False)
    wire.publish.assert_not_called()


@pytest.mark.parametrize('protocol', ['v2', 'legacy'])
@pytest.mark.parametrize('mapped,model', [(False, None), (True, None), (True, 'P1S')])
def test_rack_observed_during_upload_refuses_next_dispatch(review_db, tmp_path, monkeypatch, protocol, mapped, model):
    from modules.printers import dispatch
    from tests.test_contracts.test_multi_source_ams_dispatch import owned_read, setup_multi, transport
    events = []
    if mapped:
        engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='P1S')
        if model is None:
            with engine.begin() as conn:
                conn.execute(text('UPDATE printers SET model=NULL,machine_type=NULL WHERE id=59'))
        owned_read(monkeypatch, engine, report, events)
    else:
        engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu')
    def report_rack():
        with engine.begin() as conn:
            conn.execute(text("UPDATE printers SET installed_nozzles=:rows WHERE id=59"),
                         {'rows': STORED_RACK_ROWS})
    sent = transport(monkeypatch, path, protocol, events, after_upload=report_rack)
    ok, reason = dispatch.dispatch_job(59, 53)
    assert ok, reason
    ok, reason = dispatch.dispatch_job(59, 54)
    if model == 'P1S':
        assert not reason.startswith('unsupported_rack_printer:')
    else:
        assert not ok and reason.startswith('unsupported_rack_printer:')
    assert events == (['upload', 'fresh_read', 'start'] if mapped else ['upload', 'start'])
    assert len(sent) == 1


@pytest.mark.parametrize('transport', ['legacy', 'v2-monitor', 'v2-command'])
def test_rack_monitoring_requests_reach_wire(transport, offline_store, monkeypatch):
    from unittest.mock import Mock
    from core import db
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter

    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
    offline_store.execute(text("UPDATE printers SET model='H3C' WHERE id=199"))
    offline_store.commit()
    config = BambuAdapterConfig('199', 'synthetic', '127.0.0.1', '')
    wire = Mock()
    wire.publish.return_value.rc = 0
    wire.publish.return_value.is_published.return_value = True
    expected = [('pushing', 'pushall'), ('info', 'get_version')]
    if transport == 'legacy':
        adapter = BambuPrinter(config.host, config.serial, '', printer_id=199)
    elif transport == 'v2-monitor':
        adapter = BambuTelemetryAdapter(config, emitter=lambda event: None)
    else:
        adapter = BambuCommandAdapter(config)
        expected = [('pushing', 'pushall')]
    adapter._client = wire
    adapter._connected = True
    assert adapter.request_full_status()
    if transport == 'v2-monitor':
        assert adapter.request_version()
    assert [json.loads(call.args[1]) for call in wire.publish.call_args_list] == [
        {section: {'command': command, 'sequence_id': '0'}} for section, command in expected]


def test_unidentified_rack_keeps_refusal_across_partial_reports(offline_store, v2_offline_monitor):
    from modules.printers.printer_models import rack_printer_refusal
    monitor, _, runtime = v2_offline_monitor
    monitor._last_heartbeat = runtime.time.time()
    # No product name or known rack addresses. Only a fresh, complete report counts.
    entries = [{'id': n} for n in (16, 20, 24)]
    packet = {'print': {'device': {'nozzle': {'info': entries}}}}
    def refusal():
        row = offline_store.execute(text('SELECT model,machine_type,installed_nozzles FROM printers WHERE id=199')).mappings().one()
        return rack_printer_refusal(row)
    monitor._on_status(SimpleNamespace(raw_data=packet))
    assert refusal() is None
    packet['print']['device']['nozzle']['exist'] = sum(1 << row['id'] for row in entries)
    monitor._on_status(SimpleNamespace(raw_data=packet, mqtt_retained=True))
    assert refusal() is None
    monitor._on_status(SimpleNamespace(raw_data=packet))
    assert refusal().startswith('unsupported_rack_printer:')
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'nozzle_type': 'HS01-0.4'}}))
    assert refusal().startswith('unsupported_rack_printer:')


def test_legacy_nowait_cannot_send_a_control_with_a_status_request(offline_store, monkeypatch):
    from unittest.mock import Mock
    from core import db
    from modules.printers.adapters.bambu import BambuPrinter

    monkeypatch.setattr(db, 'engine', offline_store.get_bind())
    offline_store.execute(text("UPDATE printers SET model='N10' WHERE id=199"))
    offline_store.commit()
    sender = BambuPrinter('127.0.0.1', 'synthetic', '', printer_id=199)
    sender._client = Mock()
    sender._client.publish.return_value.rc = 0
    sender._connected = True
    assert not sender._publish_nowait({'pushing': {'sequence_id': '0', 'command': 'pushall'},
                                       'print': {'sequence_id': '0', 'command': 'pause'}})
    sender._client.publish.assert_not_called()


def test_setting_known_fixed_model_clears_old_rack_evidence(offline_store, token_client):
    from modules.printers import routes_crud
    rows = [{'nozzle_id': i, 'side': 'rack', 'position': i - 15} for i in range(16, 19)]
    offline_store.execute(text("UPDATE printers SET installed_nozzles=:rows WHERE id=199"), {'rows': json.dumps(rows)})
    offline_store.commit()
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    response = client.patch('/printers/199', headers=issue(['admin', 'write:printers']), json={'model': 'H2C'})
    assert response.status_code == 200
    assert response.json()['model'] == 'H2C' and response.json()['installed_nozzles'] == []
    stored = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
    assert json.loads(stored) == []


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
def test_mounted_tool_overrides_lagging_source(capture):
    from modules.printers.nozzle_capability import installed_nozzles
    capture['device']['nozzle']['src_id'] = 19
    rows = installed_nozzles(capture, include_rack=True)
    assert [(r['side'], r.get('position'), r.get('nozzle_id')) for r in rows] == [
        ('rack', n, n + 15) for n in range(1, 10)]
    assert [r['mounted'] for r in rows] == [n == 5 for n in range(1, 10)]
    assert (rows[4]['type'], rows[4]['present'], rows[4]['virtual_slot_id']) == ('HS01', True, 251)


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
def test_loaded_capture_printer_views(capture, v2_offline_monitor, offline_store, token_client, monkeypatch):
    from modules.printers import routes_crud
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.printers.telemetry.events import BambuReportEvent
    from modules.printers import routes_ams_env, routes_integrations, routes_status
    from modules.printers.telemetry import feature_flag
    capture['gcode_state'] = 'RUNNING'
    capture['ams']['tray_now'] = '6'
    capture['hms'] = [{'attr': 0x0300E400, 'code': 0x00020007}]
    monitor, emit, runtime = v2_offline_monitor
    from modules.notifications import error_handling
    monkeypatch.setattr(error_handling, 'get_db', runtime.get_db)
    outgoing = []
    monkeypatch.setattr(error_handling, 'dispatch_alert', lambda **kwargs: outgoing.append(kwargs))
    offline_store.execute(text("UPDATE printers SET model='H3C',tags='[\"rack\"]' WHERE id=199"))
    offline_store.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                               "VALUES(199,1,'PETG','112233')"))
    offline_store.commit()
    emit[0](BambuReportEvent('monitor-199', 2000, BambuPrintSection.model_validate(capture), False))
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    headers = issue(['read:printers'])
    listed = client.get('/printers', params={'tag': 'rack'}, headers=headers).json()
    for printer in (next(p for p in listed if p['id'] == 199), client.get('/printers/199', headers=headers).json()):
        rows = printer['installed_nozzles']
        assert [r['position'] for r in rows] == list(range(1, 10))
        assert [r['position'] for r in rows if r['mounted']] == [5]
        slots = sorted(printer['filament_slots'], key=lambda s: s['slot_number'])
        assert [s['slot_number'] for s in slots] == list(range(1, 10))
        assert printer['feeding_slot'] == 7
        assert printer['last_error_code'] is None
        notice, = json.loads(printer['hms_errors'])
        assert notice['severity'] == 'info'
        assert notice['message'] == 'Printer code 0300-E400-0002-0007 (no description; the printer reports no error)'
        loaded = {1: ('PLA', 'GFA01', 'AE96D4FF'), 7: ('PLA', 'GFA01', 'AE835BFF'), 9: ('PLA', 'GFA00', 'FF6A13FF')}
        for n, slot in enumerate(slots, 1):
            assert slot['rack_filament'] is None
            if n in loaded:
                material, preset, rgba = loaded[n]
                assert (slot['filament_type'], slot['preset_id'], slot['color_hex'], slot['remaining']) == (material, preset, rgba[:6], 100)
                assert slot['assigned_spool_id'] and slot['spool_confirmed']
            else:
                assert (slot['filament_type'], slot['color_hex'], slot['assigned_spool_id']) == ('empty', None, None)
    for module in (routes_integrations, routes_status, routes_ams_env):
        client.app.include_router(module.router)
    assert not outgoing
    home = client.get('/integrations/home-assistant/status', headers=issue(['read'])).json()
    ha = next(p for p in home['printers'] if p['id'] == 199)
    assert ha['active_errors'] == 0 and ha['status'] == 'printing' and ha['last_error'] is None
    assert len(ha['slots']) == 9
    for slot in ha['slots']:
        if slot['slot'] in loaded:
            _, preset, rgba = loaded[slot['slot']]
            assert (slot['preset_id'], slot['color_hex'].lstrip('#').upper(), slot['remaining_percent']) == (preset, rgba[:6], 100)
        else:
            assert slot['remaining_percent'] is None and slot['color_hex'] is None
    expected = [('Slots 6, 8', 2, 36, 25.4), ('Slots 1-5, 7, 9', 3, 30, 25.0)]
    assert [(u['label'], u['bambu_level'], u['humidity_percent'], u['temperature_c']) for u in ha['ams']] == expected
    current = client.get('/printers/199/ams/current', headers=headers).json()
    assert [(u['label'], u['humidity'], u['humidity_percent'], u['temperature']) for u in current['units']] == expected
    history = client.get('/printers/199/ams/environment', headers=headers).json()
    assert list(history['labels'].values()) == [entry[0] for entry in expected]
    assert [len(rows) for rows in history['units'].values()] == [1, 1]

    monkeypatch.setattr(routes_status.crypto, 'decrypt', lambda _: 'synthetic|synthetic')
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: True)
    monkeypatch.setattr(routes_status, '_fetch_live_status_v2', lambda *args: {'gcode_state': 'RUNNING'})
    offline_store.execute(text("UPDATE printers SET api_host='test.invalid',api_key='synthetic' WHERE id=199"))
    offline_store.commit()
    live = client.get('/printers/199/live-status', headers=headers).json()
    assert live['feeding_slot'] == 7
    assert [(s['slot_number'], s['color_hex'], s['remaining']) for s in live['filament_slots']] == [
        (s['slot_number'], s['color_hex'], s['remaining']) for s in slots]


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
@pytest.mark.parametrize('after_idle', [False, True])
def test_mounted_report_refuses_dispatch(capture, after_idle, offline_store, v2_offline_monitor, tmp_path, monkeypatch):
    from modules.printers import dispatch
    configure_dispatch(offline_store, tmp_path, monkeypatch, 'bambu')
    monitor, _, _ = v2_offline_monitor
    monitor.printer_id = 59
    if after_idle:
        monitor._on_status(SimpleNamespace(raw_data={'print': capture_packet('idle')}))
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': capture}))
    ok, reason = dispatch.dispatch_job(59, 53)
    assert not ok and reason.startswith('unsupported_rack_printer:')
    stored = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=59')).scalar_one()
    assert [(r['position'], r['mounted']) for r in json.loads(stored)] == [(n, n == 5) for n in range(1, 10)]


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
def test_ams3h_filament_deltas(capture, offline_store, v2_offline_monitor, monkeypatch):
    monitor, _, runtime = v2_offline_monitor
    emit_rack_report(monitor, offline_store, capture)
    def reading():
        raw = offline_store.execute(text('SELECT tray_observation FROM filament_slots WHERE printer_id=199 '
                                         'AND slot_number=7')).scalar_one()
        return json.loads(raw)['rack_filament']

    def report(tray, *, vir=None, age=0, heartbeat=True):
        packet = {'ams': {'ams': [{'id': '32', 'tray': [{'id': '6', **tray}]}]}}
        if vir is not None:
            packet['vir_slot'] = [{'id': '249', **vir}]
        monitor._last_heartbeat = 0 if heartbeat else runtime.time.time()
        monitor._on_status(SimpleNamespace(raw_data={'print': packet}, report_age_seconds=age))
    initial = reading()
    assert initial['color'] == 'AE835BFF'
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(time=lambda: 2011))
    # Coalesce an older colour with the next fresh type before the next database write.
    report({'tray_color': '123456FF'}, age=12, heartbeat=False)
    assert reading() == initial
    report({'tray_type': 'ABS'}, vir={'tray_color': '987654FF'})
    changed = reading()
    assert (changed['type'], changed['preset'], changed['color']) == ('ABS', 'GFA01', 'AE835BFF')
    report({'state': 0})
    assert tuple(reading()[k] for k in ('type', 'preset', 'color')) == ('', '', '987654FF')
    report({'state': 0}, vir={'tray_color': ''})
    assert not any(reading().get(k) for k in ('type', 'preset', 'color'))
    report({'state': 0}, vir={'tray_type': 'TPU', 'tray_info_idx': 'fallback', 'tray_color': '654321FF'})
    assert reading()['type'] == 'TPU'


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
def test_ams3h_unit_is_excluded_from_generic_ams(capture, offline_store, v2_offline_monitor):
    from modules.printers.services import bambu_tray_counts
    monitor, _, _ = v2_offline_monitor
    assert bambu_tray_counts(capture) == {'ams_slots': 0, 'ams_ht_slots': 0}
    # The capture has only zone sensors. A unit-level reading must not create ordinary AMS unit 0.
    capture['ams']['ams'][0]['temp'] = '25'
    emit_rack_report(monitor, offline_store, capture)
    assert bambu_tray_counts(capture, 'N10') == {'ams_slots': 9, 'ams_ht_slots': 0}
    assert offline_store.execute(text('SELECT ams_unit,temperature FROM ams_telemetry WHERE printer_id=199 ORDER BY ams_unit')).all() == [(0, 25.4), (1, 25.0)]
    assert monitor._ams_sync_failures == 0
    loaded = {1: "AE96D4", 7: "AE835B", 9: "FF6A13"}
    assert offline_store.execute(text('SELECT slot_number,filament_type,color_hex FROM filament_slots '
                                      'WHERE printer_id=199 ORDER BY slot_number')).all() == [
        (n, 'PLA' if n in loaded else 'empty', loaded.get(n)) for n in range(1, 10)]


@pytest.mark.parametrize('other_ids', [[], list(range(16, 25)), [1, 16, 17]])
def test_fixed_nozzle_with_address_keeps_its_identity(other_ids):
    from modules.printers.nozzle_capability import installed_nozzles
    ids = [0, *other_ids]
    info = [{'id': i, 'addr_id': 4} for i in ids]
    packet = {'device': {'nozzle': {'info': info, 'exist': sum(1 << i for i in ids)}}}
    fixed = installed_nozzles(packet, observed_at='now')
    rows = installed_nozzles(packet, include_rack=True, observed_at='now')
    assert [r for r in rows if r['side'] != 'rack'] == fixed
    assert fixed[0]['extruder'] == 0 and fixed[0]['present'] is True


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
def test_virtual_slot_delta_preserves_ams_readings(capture, offline_store, v2_offline_monitor, monkeypatch):
    monitor, _, runtime = v2_offline_monitor
    emit_rack_report(monitor, offline_store, capture)

    def loaded():
        rows = offline_store.execute(text('SELECT slot_number,tray_observation FROM filament_slots '
                                          'WHERE printer_id=199 AND slot_number IN (1,7,9)'))
        return {n: json.loads(raw)['rack_filament'] for n, raw in rows}

    initial = loaded()
    assert {n: r['color'] for n, r in initial.items()} == {1: 'AE96D4FF', 7: 'AE835BFF', 9: 'FF6A13FF'}
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(time=lambda: 2011))
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'vir_slot': capture['vir_slot']}}))
    assert loaded() == initial
    # This AMS update predates the virtual delta but is newer than the last AMS reading.
    monitor._last_heartbeat = 0
    tray = {'id': '6', 'tray_color': '123456FF'}
    monitor._on_status(SimpleNamespace(raw_data={'print': {'ams': {'ams': [{'id': '32', 'tray': [tray]}]}}},
                                       report_age_seconds=5))
    assert loaded()[7]['color'] == '123456FF'


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
@pytest.mark.parametrize('loaded_ams', [False, True])
def test_hand_fed_reading_behind_ams_tray(capture, loaded_ams, offline_store, v2_offline_monitor):
    monitor, _, _ = v2_offline_monitor
    virtual = next(t for t in capture['vir_slot'] if t['id'] == '249')
    virtual.update(tray_type='TPU', tray_info_idx='hand-fed', tray_color='654321FF')
    if not loaded_ams:
        tray = next(t for t in capture['ams']['ams'][0]['tray'] if t['id'] == '6')
        tray.clear()
        tray.update(id='6', tray_type='', tray_color='00000000')
    emit_rack_report(monitor, offline_store, capture)

    def filament():
        raw = offline_store.execute(text('SELECT tray_observation FROM filament_slots WHERE printer_id=199 '
                                         'AND slot_number=7')).scalar_one()
        reading = json.loads(raw)['rack_filament']
        return tuple(reading.get(k) for k in ('type', 'preset', 'color'))

    hand_fed = ('TPU', 'hand-fed', '654321FF')
    if loaded_ams:
        assert filament() == ('PLA', 'GFA01', 'AE835BFF')
        monitor._last_heartbeat = 0
        monitor._on_status(SimpleNamespace(raw_data={'print': {
            'ams': {'ams': [{'id': '32', 'tray': [{'id': '6', 'state': 0}]}]}}}))
    assert filament() == hand_fed


@pytest.mark.parametrize('capture', ['ams3h-loaded'], indirect=True)
def test_duplicate_parked_row_cannot_establish_mounted_rack(capture, offline_store, v2_offline_monitor):
    from modules.printers.printer_models import rack_printer_refusal
    rows = capture['device']['nozzle']['info']
    rows.append(next(row.copy() for row in rows if row['id'] == 16))
    monitor, _, _ = v2_offline_monitor
    monitor._on_status(SimpleNamespace(raw_data={'print': capture}))
    printer = offline_store.execute(text('SELECT model,machine_type,installed_nozzles FROM printers '
                                         'WHERE id=199')).mappings().one()
    assert rack_printer_refusal(printer) is None
