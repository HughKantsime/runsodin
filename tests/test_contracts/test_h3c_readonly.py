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


@pytest.fixture
def capture(request, monkeypatch, review_db):
    from core import db
    # The full packet also wakes the monitor's HMS worker. Keep it on the owned database.
    monkeypatch.setattr(db, 'engine', review_db.get_bind())
    name = getattr(request, 'param', 'idle')
    path = Path(__file__).parents[1] / f'fixtures/telemetry/h3c-{name}-2026-11-21.json'
    return json.loads(path.read_text())['message']['print']


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
        assert all(slot['rack_filament']['type'] == '' and slot['observed_state'] is None and slot['filament_type'] == 'empty'
                   for slot in printer['filament_slots'])
    status = client.get('/printers/199/nozzle-status', headers=headers).json()
    assert status['installed_nozzles'] == public and status['nozzle_count'] == 1
    assert offline_store.execute(text('SELECT slot_number,color FROM filament_slots WHERE printer_id=199 ORDER BY slot_number')).all() == [
        (n, 'Blue' if n == 2 else None) for n in range(1, 10)]


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
    assert {json.loads(raw)['rack_filament']['id'] for raw in observations} == set(range(247, 256))
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
    ('H3C', 'P1S', 'Bambu Lab P1S'), ('P1S', 'Bambu Lab H3C', 'Bambu Lab H3C'), ('H3C', None, None)])
def test_dispatch_refuses_target_before_device_or_file_inspection(review_db, tmp_path, monkeypatch, model, machine, file_model):
    from modules.printers import dispatch, smart_plug
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu')
    if file_model:
        with ZipFile(path, 'a') as package:
            package.writestr('Metadata/project_settings.config', json.dumps({'printer_model': file_model}))
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET model=:model,machine_type=:machine WHERE id=59"), {'model': model, 'machine': machine})
    monkeypatch.setattr(dispatch, '_validated_print_options', lambda *a: pytest.fail('Reached file inspection'))
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', lambda *a: pytest.fail('Reached device preparation'))
    ok, reason = dispatch.dispatch_job(59, 53)
    assert not ok and reason.startswith('unsupported_rack_printer:')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0


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
