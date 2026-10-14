"""Actual monitor speed observations to committed live WebSocket output."""
from types import SimpleNamespace
from contextlib import contextmanager
import pytest
from tests.test_contracts.test_printer_offline_behavior import offline_store,review_db,v2_offline_monitor


def test_reported_speed_survives_actual_monitor_to_websocket(v2_offline_monitor,monkeypatch):
    monitor,_,module=v2_offline_monitor
    events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append((kind,payload)))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'RUNNING','spd_lvl':3,'spd_mag':124}}))
    assert events[-1][1]['speed_level']==3
    assert events[-1][1]['speed_percent']==124


@pytest.mark.parametrize('invalid',[None,True,'invalid',1.5,float('nan'),float('inf'),-1,10**1000],ids=['null','bool','text','fraction','nan','infinity','negative','oversized'])
def test_invalid_or_partial_speed_report_preserves_last_valid_live_values(v2_offline_monitor,monkeypatch,invalid):
    monitor,_,module=v2_offline_monitor;events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append(payload))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','spd_lvl':3,'spd_mag':124}}))
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'spd_lvl':invalid,'spd_mag':invalid}}))
    assert events[-1]['speed_level']==3 and events[-1]['speed_percent']==124


def test_throttled_speed_and_explicit_zero_flush_on_next_committed_heartbeat(v2_offline_monitor,monkeypatch):
    monitor,_,module=v2_offline_monitor;events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append(payload))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE'}}))
    assert events[-1]['speed_level'] is None and events[-1]['speed_percent'] is None
    events.clear()
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2001))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'spd_lvl':'1','spd_mag':'0'}}))
    assert events==[]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{}}))
    assert events[-1]['speed_level']==1 and events[-1]['speed_percent']==0


def test_speed_observations_do_not_escape_failed_heartbeat_commit(v2_offline_monitor,monkeypatch):
    monitor,_,module=v2_offline_monitor;original=module.get_db;failed=[False];events=[]
    class Connection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def commit(self):
            if not failed[0]:
                failed[0]=True;self.conn.rollback();raise RuntimeError('Synthetic commit refusal')
            self.conn.commit()
    @contextmanager
    def database():
        with original() as conn:yield Connection(conn)
    monkeypatch.setattr(module,'get_db',database)
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append(payload))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','spd_lvl':4,'spd_mag':166}}))
    assert events==[]
    monitor._on_status(SimpleNamespace(raw_data={'print':{}}))
    assert events[-1]['speed_level']==4 and events[-1]['speed_percent']==166


@pytest.mark.parametrize('retirement',['retired','old-generation'])
def test_retired_speed_callback_cannot_change_state_or_publish(v2_offline_monitor,monkeypatch,retirement):
    monitor,_,module=v2_offline_monitor;events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append(payload))
    if retirement=='retired':monitor._connection_retired=True;generation=None
    else:generation=monitor._connection_generation-1
    monitor._on_status(SimpleNamespace(raw_data={'print':{'spd_lvl':3,'spd_mag':124}}),generation=generation)
    assert events==[] and 'spd_lvl' not in monitor._state and 'spd_mag' not in monitor._state


@pytest.mark.parametrize('mode',[0,5,2.0,'5'])
def test_speed_modes_outside_protocol_levels_are_unknown(mode):
    from modules.printers.monitors.mqtt_telemetry import parse_bambu_speed
    assert parse_bambu_speed({'spd_lvl':mode,'spd_mag':100})=={'speed_level':None,'speed_percent':100}


@pytest.mark.parametrize('field',['spd_lvl','spd_mag'])
@pytest.mark.parametrize('invalid',[True,False,2.0,1.5,'2.0'],ids=['true','false','whole-float','fraction','decimal-text'])
def test_typed_speed_decoder_refuses_values_that_would_invent_integer_observations(field,invalid):
    from pydantic import ValidationError
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    with pytest.raises(ValidationError):
        BambuPrintSection.model_validate({field:invalid})


@pytest.mark.parametrize('level,percent',[(3,124),('1','0'),(None,None)])
def test_typed_report_bridge_preserves_measured_speed_to_actual_websocket(v2_offline_monitor,monkeypatch,level,percent):
    from modules.printers.telemetry.events import BambuReportEvent
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    monitor,emitters,module=v2_offline_monitor;events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append(payload))
    section=BambuPrintSection.model_validate({'gcode_state':'IDLE','spd_lvl':level,'spd_mag':percent})
    emitters[0](BambuReportEvent('monitor-199',2000,section))
    assert events[-1]['speed_level']==(None if level is None else int(level))
    assert events[-1]['speed_percent']==(None if percent is None else int(percent))
