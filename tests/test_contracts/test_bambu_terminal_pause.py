import logging
from types import SimpleNamespace
import pytest

from modules.printers.monitors import terminal_evidence, pause_cause
from modules.printers.hms_codes import lookup_print_error
from modules.notifications.error_handling import parse_hms_errors


@pytest.mark.parametrize('terminal', ['FAILED', 'FINISH'])
def test_terminal_evidence_is_bounded_and_keeps_print_identity(monkeypatch, caplog, terminal):
    clock = [0]
    monkeypatch.setattr(terminal_evidence.time, 'monotonic', lambda: clock[0])
    monitor = SimpleNamespace(printer_id=9, _current_job_id=123)
    logger = logging.getLogger('terminal-test')
    caplog.set_level(logging.INFO, logger='terminal-test')
    packet = {'gcode_state':terminal, 'print_error':0, 'hms':[{'attr':1,'code':2}], 'stg_cur':6,
              'serial':'DO-NOT-LOG', 'access_code':'DO-NOT-LOG'}
    terminal_evidence.capture(monitor, packet, logger)
    monitor._current_job_id = None
    for _ in range(25): terminal_evidence.capture(monitor, packet, logger)
    assert len(caplog.records) == 20
    assert all('print_id=123' in record.message for record in caplog.records)
    assert 'DO-NOT-LOG' not in caplog.text
    clock[0] = 31
    terminal_evidence.capture(monitor, packet, logger)
    assert len(caplog.records) == 20  # never re-arm the same terminal state
    terminal_evidence.capture(monitor, {'gcode_state':'RUNNING'}, logger)
    monitor._current_job_id = 124
    terminal_evidence.capture(monitor, packet, logger)
    clock[0] = 61
    terminal_evidence.capture(monitor, packet, logger)
    assert len(caplog.records) == 21
    assert 'print_id=124' in caplog.records[-1].message


def test_terminal_evidence_logs_raw_deltas_not_cached_errors(caplog):
    caplog.set_level(logging.INFO)
    monitor = SimpleNamespace(printer_id=9, _current_job_id=123)
    terminal_evidence.capture(monitor, {'gcode_state':'FAILED','print_error':123}, logging.getLogger('delta'))
    terminal_evidence.capture(monitor, {'print_error':456}, logging.getLogger('delta'))
    assert 'gcode_state=None print_error=456' in caplog.records[-1].message


def test_pause_prefers_current_error_and_never_historical_last_error():
    monitor = SimpleNamespace(_state={'gcode_state':'PAUSE','print_error':0x07008011}, last_error_message='stale fan')
    pause_cause.observe(monitor, {'hms':[{'attr':0x03003100,'code':1}]})
    assert pause_cause.display_stage(pause_cause.stage(monitor, 'Paused')) == 'Paused: ' + lookup_print_error('0700_8011')
    monitor._state['gcode_state'] = 'RUNNING'
    assert pause_cause.stage(monitor, 'Printing') == 'Printing'


def test_pause_uses_latest_arriving_still_active_hms_and_clears_removed_codes():
    monitor = SimpleNamespace(_state={'gcode_state':'PAUSE','print_error':0})
    old, new = {'attr':0x03003100,'code':1}, {'attr':0x07002300,'code':0x20001}
    pause_cause.observe(monitor, {'hms':[old]})
    pause_cause.observe(monitor, {'hms':[new, old]})
    assert pause_cause.display_stage(pause_cause.stage(monitor, '')) == 'Paused: ' + parse_hms_errors([new])[0]['message']
    pause_cause.observe(monitor, {})  # partial packet does not clear an active report
    assert pause_cause.display_stage(pause_cause.stage(monitor, '')) == 'Paused: ' + parse_hms_errors([new])[0]['message']
    pause_cause.observe(monitor, {'hms':[old]})
    assert pause_cause.display_stage(pause_cause.stage(monitor, '')) == 'Paused: ' + parse_hms_errors([old])[0]['message']
    pause_cause.observe(monitor, {'hms':[]})
    assert pause_cause.stage(monitor, '') == 'Paused: Cause unavailable'


def test_numeric_string_terminal_codes_are_preserved(caplog):
    caplog.set_level(logging.INFO)
    monitor = SimpleNamespace(printer_id=9, _current_job_id=123)
    terminal_evidence.capture(monitor, {'gcode_state':'FAILED','print_error':'83886094','stg_cur':'6'}, logging.getLogger('delta'))
    assert 'print_error=83886094' in caplog.text and 'stg_cur=6' in caplog.text


@pytest.mark.parametrize('error,status', [(0,'failed'),(0x0300400C,'cancelled'),(0x0500400E,'cancelled'),(1,'failed')])
def test_cancel_classification_is_unchanged(monkeypatch, error, status):
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    from core import job_timeline
    monkeypatch.setattr(job_timeline, 'close_pause_episode', lambda *args: None)
    monitor = PrinterMonitor(9,'Synthetic','','','')
    monitor._current_job_id = 123
    monitor._state = {'print_error':error}
    outcomes = []
    monkeypatch.setattr(monitor, '_job_ended', outcomes.append)
    monitor._on_state_change('RUNNING','FAILED')
    assert outcomes == [status]

# Exercise the real packet ingestion and owned database, not only pure helpers.
from tests.test_contracts.test_daemon_ams_inventory import daemon_ams
from tests.test_contracts.test_ams_sync_acceptance import sync_client, own_ams_operator, ams_client, token_client, review_db


def test_monitor_persists_pause_cause_and_logs_terminal_packet(daemon_ams, review_db, caplog):
    from sqlalchemy import text
    _, monitor, _ = daemon_ams
    caplog.set_level(logging.INFO, logger='mqtt_monitor')
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state':'PAUSE','print_error':0x07008011}}))
    stored = review_db.execute(text('SELECT print_stage FROM printers WHERE id=199')).scalar_one()
    assert stored == 'Paused: print_error=07008011'
    assert pause_cause.display_stage(stored) == 'Paused: ' + lookup_print_error('0700_8011')
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state':'FAILED','print_error':0}}))
    assert 'Bambu terminal evidence printer_id=199' in caplog.text


def test_monitor_retains_reported_preset_temperatures(daemon_ams, review_db):
    import json
    from sqlalchemy import text
    send, _, _ = daemon_ams
    send(trays=[{'id':'0','tray_type':'PETG','tray_info_idx':'GFG02','nozzle_temp_min':'230',
                 'nozzle_temp_max':'260','tray_color':'FFFFFFFF','tag_uid':''}])
    state = json.loads(review_db.execute(text('SELECT tray_observation FROM filament_slots WHERE id=199')).scalar_one())
    assert state['reported_preset'] == {'tray_info_idx':'GFG02','tray_type':'PETG','nozzle_temp_min':'230','nozzle_temp_max':'260'}


def test_pause_api_decodes_full_text_from_bounded_storage():
    from modules.printers.schemas import PrinterResponse
    monitor = SimpleNamespace(_state={'gcode_state':'PAUSE','print_error':0})
    pause_cause.observe(monitor, {'hms':[{'attr':0x07002300,'code':0x20001}]})
    stored = pause_cause.stage(monitor, 'Paused')
    assert len(stored) <= 50
    printer = PrinterResponse.model_construct(gcode_state='PAUSE', print_stage=stored)
    assert printer.model_dump()['print_stage'] == pause_cause.display_stage(stored)
    printer.gcode_state = 'RUNNING'
    printer.print_stage = 'Printing'
    assert printer.model_dump()['print_stage'] == 'Printing'


def test_partial_report_preserves_temperatures_only_for_the_same_preset():
    from core.tray_identity import advance
    tray = {'tray_info_idx':'GFG02','tray_type':'PETG','nozzle_temp_min':'230','nozzle_temp_max':'260'}
    state, _ = advance({}, tray)
    partial = {'tray_info_idx':'GFG02','tray_type':'PETG'}
    again, _ = advance(state, partial)
    assert again['reported_preset'] == tray
    assert again['id'] == state['id']
    different, _ = advance(state, {**partial,'tray_info_idx':'GFG99'})
    assert different['reported_preset']['nozzle_temp_min'] is None
