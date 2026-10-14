"""Malformed external-spool reports must not crash telemetry or fabricate quantity."""
import pytest
from modules.printers.monitors.mqtt_telemetry import parse_h2d_external_spools
from types import SimpleNamespace
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor


@pytest.mark.parametrize('value', [True, False, 12.5, 'bad', '12.5', float('inf'), float('nan')],
                         ids=['true','false','fraction','text','fraction-text','infinity','nan'])
def test_external_spool_invalid_remaining_is_unknown_and_keeps_other_side(value):
    result=parse_h2d_external_spools({'vt_tray':[
        {'tray_type':'PLA','remain':value}, {'tray_type':'PETG','remain':0}]})
    assert result['left']['remain_percent'] is None
    assert result['right']['remain_percent']==0


def test_external_spool_missing_remaining_is_unknown_not_empty():
    assert parse_h2d_external_spools({'vt_tray':{'tray_type':'PLA'}})['left']['remain_percent'] is None


@pytest.mark.parametrize('value', [7, True, [], {}], ids=['number','bool','list','dict'])
def test_external_spool_invalid_color_preserves_material_and_quantity(value):
    result=parse_h2d_external_spools({'vt_tray':{'tray_type':'PLA','tray_color':value,'remain':25}})
    assert result['left']=={'material':'PLA','color':'','remain_percent':25}


@pytest.mark.parametrize('value,expected', [(0,0),('0',0),('70',70),(-1,-1),(None,None)])
def test_external_spool_valid_integer_compatibility(value,expected):
    assert parse_h2d_external_spools({'vt_tray':{'tray_type':'PLA','remain':value}})['left']['remain_percent']==expected


def test_invalid_external_spool_does_not_suppress_actual_committed_websocket(v2_offline_monitor,offline_store,monkeypatch):
    monitor,_,module=v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=199"));offline_store.commit()
    events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append((kind,payload)))
    monitor._on_status(SimpleNamespace(raw_data={'print':{
        'gcode_state':'IDLE','nozzle_temper_1':190,
        'ams':{'vt_tray':[{'tray_type':'PLA','remain':'bad'}, {'tray_type':'PETG','remain':0}]}}}))
    assert offline_store.execute(text('SELECT nozzle_1_temp FROM printers WHERE id=199')).scalar_one()==190
    assert events
    assert events[-1][1]['external_spools']['left']['remain_percent'] is None
    assert events[-1][1]['external_spools']['right']['remain_percent']==0
