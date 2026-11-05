"""Restart safety at the real detection and adapter-command boundaries."""
import pytest
from types import SimpleNamespace
from sqlalchemy import text

from tests.test_contracts.test_vision_decision_behavior import vision, review_db


@pytest.fixture
def guarded_vision(vision, review_db, monkeypatch):
    from core import monitor_observations
    from modules.notifications import event_dispatcher
    from modules.printers.adapters import moonraker
    thread, model, module = vision
    clock = [1000.0]
    fake_time = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    monkeypatch.setattr(module, 'time', fake_time)
    monkeypatch.setattr(monitor_observations, 'time', fake_time)
    monkeypatch.setattr(module, 'PROCESS_STARTED_AT', 999)
    monkeypatch.setattr(module, 'engine', review_db.get_bind())
    thread.current_layer = 1
    model.has_model = lambda name: name == 'first_layer'
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='synthetic.invalid',gcode_state='RUNNING' WHERE id=9"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,status,started_at) VALUES(23,9,'current.3mf','running',CURRENT_TIMESTAMP)"))
    review_db.commit()
    pauses, records, alerts = [], [], []
    class Adapter:
        def __init__(self, host): assert host == 'synthetic.invalid'
        def pause_print(self):
            pauses.append('pause')
            return True
    monkeypatch.setattr(moonraker, 'MoonrakerPrinter', Adapter)
    monkeypatch.setattr(module.frame_storage, 'save_detection_frame', lambda *a: 'synthetic.jpg')
    monkeypatch.setattr(thread, '_insert_detection', lambda *a: records.append(a) or 1)
    monkeypatch.setattr(event_dispatcher, 'dispatch_alert', lambda **kw: alerts.append(kw))
    monkeypatch.setattr(module, 'ws_push', lambda *a: None)
    def report(state='RUNNING', **kwargs):
        with review_db.get_bind().begin() as conn:
            monitor_observations.persist(conn, 9, state, **kwargs)
    return thread, model, clock, report, pauses, records, alerts


def test_first_frame_records_and_alerts_but_grace_requires_time_and_frames(guarded_vision):
    thread, model, clock, report, pauses, records, alerts = guarded_vision
    report()
    thread._capture_and_analyze()
    assert len(records) == len(alerts) == 1 and pauses == []
    clock[0] += 61
    report()
    thread._capture_and_analyze()
    assert pauses == []  # two frames, despite elapsed grace
    clock[0] += 61
    report()
    thread._capture_and_analyze()
    assert pauses == ['pause']


def test_three_fast_frames_still_require_sixty_seconds(guarded_vision):
    thread, _, clock, report, pauses, _, _ = guarded_vision
    report()
    for _ in range(3):
        thread._capture_and_analyze()
    thread._auto_pause()
    assert pauses == []
    clock[0] += 60
    report()
    thread._auto_pause()
    assert pauses == ['pause']


@pytest.mark.parametrize('evidence', ['missing', 'preboot', 'retained', 'expired', 'paused'])
def test_persisted_running_never_substitutes_for_live_state(guarded_vision, evidence):
    thread, _, clock, report, pauses, _, _ = guarded_vision
    if evidence == 'preboot':
        clock[0] = 998
        report()
        clock[0] = 1000
    elif evidence == 'retained':
        report(retained=True)
    elif evidence == 'expired':
        report()
    for _ in range(3):
        thread._capture_and_analyze()
    clock[0] += 61
    if evidence == 'paused':
        report('PAUSE')
    thread._auto_pause()
    assert pauses == []


def test_unknown_layer_does_not_run_first_layer_inference(guarded_vision):
    thread, model, _, report, pauses, records, _ = guarded_vision
    thread.current_layer = None
    report()
    thread._capture_and_analyze()
    assert model.calls == [] and records == [] and pauses == []
    assert thread._analyzed_frames == 0


def test_new_print_resets_grace_even_if_still_running(guarded_vision):
    thread, _, clock, report, pauses, _, _ = guarded_vision
    for _ in range(3):
        thread._capture_and_analyze()
    clock[0] += 61
    report()
    thread.update_layer(1, 24, 'RUNNING')
    thread._auto_pause()
    assert pauses == [] and thread._analyzed_frames == 0


def test_changed_database_print_identity_blocks_pause(guarded_vision, review_db):
    thread, _, clock, report, pauses, _, _ = guarded_vision
    for _ in range(3):
        thread._capture_and_analyze()
    clock[0] += 61
    report()
    review_db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=23"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,status,started_at) VALUES(24,9,'later.3mf','running',CURRENT_TIMESTAMP)"))
    review_db.commit()
    thread._auto_pause()
    assert pauses == []


def test_state_is_reread_after_adapter_preparation(guarded_vision, monkeypatch):
    from modules.printers.adapters import moonraker
    thread, _, clock, report, pauses, _, _ = guarded_vision
    for _ in range(3):
        thread._capture_and_analyze()
    clock[0] += 61
    report()
    class Adapter:
        def __init__(self, host):
            report('PAUSE')
        def pause_print(self):
            pauses.append('pause')
    monkeypatch.setattr(moonraker, 'MoonrakerPrinter', Adapter)
    thread._auto_pause()
    assert pauses == []


def test_bambu_delta_traffic_preserves_pause_authority_after_grace(guarded_vision, review_db, monkeypatch):
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from modules.printers.monitors import mqtt_printer
    from modules.printers.adapters import bambu
    from modules.printers.telemetry import feature_flag
    from core import crypto
    thread, _, clock, _, pauses, _, _ = guarded_vision
    @contextmanager
    def database():
        raw = review_db.get_bind().raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()
    monkeypatch.setattr(mqtt_printer, 'get_db', database)
    monkeypatch.setattr(mqtt_printer, 'time', SimpleNamespace(time=lambda: clock[0]))
    monitor = mqtt_printer.PrinterMonitor(9, 'Synthetic', '192.0.2.1', 'SYNTHETIC', '')
    monitor._current_job_id = 23
    monitor._last_gcode_state = 'RUNNING'
    monitor._last_heartbeat = monitor._last_spool_check = 10000
    monitor._camera_discovered = True
    def send(**packet):
        monitor._on_status(SimpleNamespace(raw_data={'print': packet}, mqtt_retained=False))
    class Command:
        def __init__(self, *args, **kwargs): pass
        def connect(self): return True
        def pause_print(self):
            pauses.append('pause')
            return True
        def disconnect(self): pass
    monkeypatch.setattr(bambu, 'BambuPrinter', Command)
    monkeypatch.setattr(crypto, 'decrypt', lambda value: 'SYNTHETIC|unused')
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: False)
    review_db.execute(text("UPDATE printers SET api_type='bambu' WHERE id=9"))
    review_db.commit()
    send(gcode_state='RUNNING')
    for _ in range(3):
        thread._capture_and_analyze()
    assert pauses == []
    for _ in range(120):
        clock[0] += 1
        send(mc_percent=20)
    assert thread._live_printing() is True
    thread._auto_pause()
    assert pauses == ['pause']
    monitor._begin_connection()
    send(mc_percent=21)
    assert thread._live_printing() is False
    thread._auto_pause()
    assert pauses == ['pause']  # reconnect cannot reuse the old explicit state
