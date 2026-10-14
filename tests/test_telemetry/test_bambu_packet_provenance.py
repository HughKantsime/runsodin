"""Actual callbacks preserve retained-message uncertainty, never invent freshness."""
import json
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('retained,expected', [(False,False),(True,True),(None,None),(1,None),('false',None)])
@pytest.mark.parametrize('path',['legacy','v2'])
def test_actual_callback_preserves_retained_provenance(path,retained,expected):
    reports=[]
    msg=SimpleNamespace(payload=json.dumps({'print':{'gcode_state':'IDLE'}}).encode())
    if retained is not None:
        msg.retain=retained
    if path=='legacy':
        from modules.printers.adapters.bambu import BambuPrinter
        adapter=BambuPrinter(ip='127.0.0.1',serial='synthetic-provenance',
                             access_code='',on_status_update=reports.append)
    else:
        from modules.printers.telemetry.bambu.adapter import BambuTelemetryAdapter,BambuAdapterConfig
        from modules.printers.telemetry.events import BambuReportEvent
        adapter=BambuTelemetryAdapter(BambuAdapterConfig('synthetic','synthetic','127.0.0.1',''),
            emitter=lambda event: reports.append(event) if isinstance(event,BambuReportEvent) else None)
    adapter._on_message(None,None,msg)
    assert len(reports)==1
    assert reports[0].mqtt_retained is expected


def test_legacy_direct_parse_does_not_inherit_false_receipt():
    from modules.printers.adapters.bambu import BambuPrinter
    observed=[]
    adapter=BambuPrinter(ip='127.0.0.1',serial='synthetic',access_code='',
        on_status_update=lambda status: observed.append(status.mqtt_retained))
    payload={'print':{'gcode_state':'IDLE'}}
    adapter._on_message(None,None,SimpleNamespace(payload=json.dumps(payload).encode(),retain=False))
    adapter._parse_status(payload)
    adapter._on_message(None,None,SimpleNamespace(payload=json.dumps(payload).encode()))
    assert observed == [False,None,None]


@pytest.mark.parametrize('packet', [
    {'gcode_state':'IDLE'}, {'ams':None}, {'ams':{'ams':[]}},
    {'ams':{'ams':[{'id':'0','tray':[{'id':'1'}]}]}},
    {'ams':{'ams':[{'id':'0','tray':[{'id':'1','tray_type':None}]}]}},
])
def test_actual_v2_monitor_shim_preserves_raw_presence(monkeypatch,packet):
    from modules.printers.telemetry.bambu import adapter as adapter_module
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.printers.telemetry.events import BambuReportEvent
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    class FakeAdapter:
        def __init__(self,config,emitter):self.emitter=emitter
        def start(self):pass
    monkeypatch.setattr(adapter_module,'BambuTelemetryAdapter',FakeAdapter)
    monitor=PrinterMonitor(199,'Synthetic','127.0.0.1','synthetic','')
    received=[]
    monkeypatch.setattr(monitor,'_recover_orphaned_jobs',lambda:None)
    monkeypatch.setattr(monitor,'_on_status',lambda status,generation: received.append(status))
    assert monitor._connect_v2()
    monitor._v2_adapter.emitter(BambuReportEvent(
        'synthetic',1.0,BambuPrintSection.model_validate(packet),mqtt_retained=True))
    assert len(received)==1
    assert received[0].raw_data['print'] == packet
    assert received[0].mqtt_retained is True
