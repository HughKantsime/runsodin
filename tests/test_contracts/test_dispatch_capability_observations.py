"""Installed hardware survives partial monitor reports and authenticated API reads."""
import json
from types import SimpleNamespace
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_dispatch_capability import REPORT


def test_nozzle_observations_persist_on_printer_api(v2_offline_monitor, offline_store, token_client, monkeypatch):
    from modules.printers import routes_status, routes_crud
    monitor, _, module = v2_offline_monitor
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', **REPORT}}))
    saved = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
    rows = json.loads(saved)
    assert [r['side'] for r in rows] == ['right','left']
    assert rows[1]['material'] == 'Tungsten carbide' and rows[1]['wear'] == 0
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE'}}))
    assert offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one() == saved
    client, issue = token_client
    client.app.include_router(routes_status.router)
    client.app.include_router(routes_crud.router)
    headers = issue(['read:printers'])
    assert client.get('/printers/199/nozzle-status',headers=headers).json()['installed_nozzles'] == rows
    assert client.get('/printers/199',headers=headers).json()['installed_nozzles'] == rows


def test_dual_monitor_never_overwrites_physical_zero_from_top_level(v2_offline_monitor, offline_store, monkeypatch):
    monitor, _, module = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=199")); offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', **REPORT}}))
    saved = offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one()
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: 2011))
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', 'nozzle_type': 'HS01', 'nozzle_diameter': .8}}))
    assert offline_store.execute(text('SELECT installed_nozzles FROM printers WHERE id=199')).scalar_one() == saved
