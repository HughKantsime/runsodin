"""Typed Bambu error values must not erase malformed wire provenance."""
import pytest
import json
from types import SimpleNamespace
from pydantic import ValidationError
from modules.printers.telemetry.bambu.raw import BambuPrintSection
from modules.printers.dispatch_readiness import raw_status_is_idle


@pytest.mark.parametrize('error', [False, True, '0', 0.0])
def test_malformed_error_refused_before_readiness_projection(error):
    with pytest.raises(ValidationError):
        BambuPrintSection.model_validate({'gcode_state':'IDLE','print_error':error})


@pytest.mark.parametrize('error,ready', [(0,True),(1,False),(-1,False),(None,False)])
def test_actual_integer_and_missing_error_readiness(error,ready):
    section = BambuPrintSection.model_validate({'gcode_state':'IDLE','print_error':error})
    assert raw_status_is_idle('bambu',section.model_dump(mode='json')) is ready


def test_actual_adapter_degrades_without_private_echo_and_recovers(caplog):
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    from modules.printers.telemetry.events import BambuReportEvent, DegradedEvent
    events = []
    adapter = BambuTelemetryAdapter(BambuAdapterConfig(printer_id='synthetic',serial='synthetic',host='127.0.0.1',access_code='',use_tls=False),events.append)
    bad = {'print':{'gcode_state':'IDLE','print_error':False,'synthetic_private_field':'synthetic private marker'}}
    adapter._on_message(None,None,SimpleNamespace(payload=json.dumps(bad).encode()))
    assert any(isinstance(event,DegradedEvent) for event in events)
    assert not any(isinstance(event,BambuReportEvent) for event in events)
    assert 'synthetic private marker' not in caplog.text
    assert all('synthetic private marker' not in repr(event) for event in events)
    events.clear()
    adapter._on_message(None,None,SimpleNamespace(payload=b'{"print":{"gcode_state":"IDLE","print_error":0}}'))
    assert any(isinstance(event,BambuReportEvent) and event.section.print_error == 0 for event in events)
