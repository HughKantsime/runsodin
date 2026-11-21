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
def capture(monkeypatch, review_db):
    from core import db
    # The full packet also wakes the monitor's HMS worker. Keep it on the owned database.
    monkeypatch.setattr(db, 'engine', review_db.get_bind())
    path = Path(__file__).parents[1] / 'fixtures/telemetry/h3c-idle-2026-11-21.json'
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
        assert printer['ams_connected'] is False and printer['filament_slots'] == [] and printer['loaded_colors'] == []
    status = client.get('/printers/199/nozzle-status', headers=headers).json()
    assert status['installed_nozzles'] == public and status['nozzle_count'] == 1
    assert offline_store.execute(text('SELECT slot_number,color FROM filament_slots WHERE printer_id=199 ORDER BY slot_number')).all() == [
        (n, 'Blue' if n == 2 else None) for n in range(1, 10)]


def test_rack_report_before_model_identification(capture, v2_offline_monitor, offline_store):
    monitor, _, _ = v2_offline_monitor
    monitor._on_status(SimpleNamespace(raw_data={'print': capture}))
    monitor._reported_model = 'Bambu Lab H3C'
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': {'nozzle_type': 'HS01-0.4'}}))
    model, stored = offline_store.execute(text('SELECT machine_type,installed_nozzles FROM printers WHERE id=199')).one()
    assert model == 'H3C'
    assert [row['position'] for row in json.loads(stored)] == list(range(1, 10))


def test_presence_uses_nozzle_id_and_position_uses_address(capture):
    from modules.printers.nozzle_capability import installed_nozzles
    rack = capture['device']['nozzle']
    rack['exist'] ^= 1 << 23
    rack['info'][7]['addr_id'], rack['info'][1]['addr_id'] = 1, 7
    rows = installed_nozzles(capture, include_rack=True)
    entry = next(row for row in rows if row.get('nozzle_id') == 23)
    assert (entry['position'], entry['type'], entry['present']) == (2, 'HT01', False)


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
