"""Actual typed adapter callback rejection and recovery, MQTT client substituted."""
import json
import pytest
from tests.test_telemetry.test_telemetry_adapter import adapter, captured_events
from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
from modules.printers.telemetry.events import DegradedEvent, BambuReportEvent
from modules.printers.telemetry.state import PrinterState

@pytest.fixture
def config():
    return BambuAdapterConfig(printer_id='synthetic-quantity',serial='synthetic-serial',host='127.0.0.1',port=1883,access_code='',use_tls=False)

@pytest.mark.parametrize('remaining', [True,False,12.5,'bad','12.5',float('inf'),float('-inf'),float('nan')])
def test_invalid_typed_ams_report_emits_degraded_without_fabricated_percentage_and_recovers(adapter, captured_events, config, remaining):
    adapter.start()
    adapter._fake.fire_connect(reason_code=0)
    bad={'print':{'gcode_state':'RUNNING','ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','remain':remaining}]}]}}}
    adapter._fake.fire_message(config.topic_report,json.dumps(bad).encode())
    assert len([event for event in captured_events if isinstance(event,DegradedEvent)])==1
    assert not [event for event in captured_events if isinstance(event,BambuReportEvent)]
    assert adapter.status().state==PrinterState.DEGRADED
    bad['print']['ams']['ams'][0]['tray'][0]['remain']=0
    adapter._fake.fire_message(config.topic_report,json.dumps(bad).encode())
    reports=[event for event in captured_events if isinstance(event,BambuReportEvent)]
    assert len(reports)==1 and reports[0].section.ams.ams[0].tray[0].remain==0
    assert adapter.status().state==PrinterState.PRINTING
    adapter.stop()
