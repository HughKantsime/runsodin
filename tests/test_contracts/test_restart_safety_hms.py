"""Retained HMS is display-only; terminal mutations require live corroboration."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_hms_observation_boundaries import (
    hms_boundary, monitor_db, STOPPING_ERROR, _running_packet_monitor,
)


def send(monitor, *, retained=False, age=None, **packet):
    monitor._on_status(SimpleNamespace(raw_data={'print': packet}, mqtt_retained=retained,
                                       report_age_seconds=age))


def state(db):
    db.expire_all()
    return db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()


def link_job(db):
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,quantity,status,printer_id,duration_hours,hold,priority) "
                    "VALUES(203,12,'Synthetic generic',1,'printing',9,1,0,3)"))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=203 WHERE id=201'))
    db.commit()


@pytest.mark.parametrize('reported', ['RUNNING', 'PAUSE', 'FAILED', 'IDLE', 'FINISH'])
def test_retained_pushall_matching_running_print_only_updates_display(hms_boundary, monkeypatch, reported):
    db, handler, _, alerts = hms_boundary
    link_job(db)
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    send(monitor, retained=True, command='push_status', gcode_state=reported,
         gcode_file='current.3mf', hms=STOPPING_ERROR)
    assert state(db) == 'running'
    assert db.execute(text('SELECT status FROM jobs WHERE id=203')).scalar_one() == 'printing'
    assert db.execute(text('SELECT hms_errors FROM printers WHERE id=9')).scalar_one()
    assert not any(a.get('alert_type') == 'print_failed' for a in alerts)
    send(monitor, mc_percent=52)
    assert state(db) == 'running'
    # An HMS-free live heartbeat cannot promote retained fault evidence.
    send(monitor, gcode_state='PAUSE', gcode_file='current.3mf')
    assert state(db) == 'running'


@pytest.mark.parametrize('reported', ['FAILED', 'PAUSE', 'IDLE'])
@pytest.mark.parametrize('level', [1, 2])
def test_live_stopping_code_and_live_stopped_state_fail(hms_boundary, monkeypatch, reported, level):
    db, _, _, alerts = hms_boundary
    link_job(db)
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    send(monitor, gcode_state=reported, gcode_file='current.3mf',
         hms=[{'attr':0x03003100,'code':(level<<16)|1}])
    assert state(db) == 'failed'
    assert db.execute(text('SELECT status FROM jobs WHERE id=203')).scalar_one() == 'failed'
    assert len([a for a in alerts if a.get('alert_type') == 'print_failed']) == 1


def test_live_code_waits_for_following_state_and_dedup_does_not_hide_it(hms_boundary, monkeypatch):
    db, _, _, _ = hms_boundary
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=STOPPING_ERROR)
    assert state(db) == 'running'
    send(monitor, gcode_state='FAILED')
    assert state(db) == 'failed'
    assert db.execute(text('SELECT error_code FROM print_jobs WHERE id=201')).scalar_one() == '03003100_00010001'


@pytest.mark.parametrize('reason', ['reconnect', 'expired', 'clock_rollback', 'new_print', 'clear', 'different_name'])
def test_pending_hms_cannot_cross_evidence_boundary(hms_boundary, monkeypatch, reason):
    db, _, _, alerts = hms_boundary
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=STOPPING_ERROR)
    if reason == 'reconnect':
        monitor._connection_generation += 1
    elif reason == 'expired':
        monitor._hms_corroboration['at'] -= 61
    elif reason == 'clock_rollback':
        monitor._hms_corroboration['at'] += 1
    elif reason == 'new_print':
        monitor._current_job_id = 202
    elif reason == 'clear':
        send(monitor, hms=[])
    elif reason == 'different_name':
        send(monitor, gcode_file='later.3mf')
    send(monitor, gcode_state='PAUSE')
    assert state(db) == 'running'
    assert not any(a.get('alert_type') == 'print_failed' for a in alerts)


@pytest.mark.parametrize('retained,age,live', [
    (True, None, 'FAILED'), (False, 61, 'FAILED'), (False, -1, 'FAILED'),
    (False, float('nan'), 'FAILED'), (False, None, None), (False, None, 'RUNNING'),
])
def test_handler_gate_fails_closed(hms_boundary, retained, age, live):
    db, handler, _, alerts = hms_boundary
    handler.process_hms_errors(9, STOPPING_ERROR, observed_filename='current.3mf',
                              mqtt_retained=retained, report_age_seconds=age, live_state=live)
    assert state(db) == 'running'
    assert not any(a.get('alert_type') == 'print_failed' for a in alerts)


def test_retained_then_identical_live_packet_is_not_deduplicated(hms_boundary, monkeypatch):
    db, _, _, _ = hms_boundary
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    send(monitor, retained=True, gcode_state='PAUSE', gcode_file='current.3mf', hms=STOPPING_ERROR)
    assert state(db) == 'running'
    send(monitor, gcode_state='PAUSE', gcode_file='current.3mf', hms=STOPPING_ERROR)
    assert state(db) == 'failed'


def test_retained_callback_cannot_retry_a_pending_terminal_write(hms_boundary, monkeypatch):
    db, handler, _, _ = hms_boundary
    original = handler._fail_active_job_for_hms
    attempts = []
    def fail_once(*args, **kwargs):
        attempts.append('write')
        return {'terminal_outcome': 'retryable'} if len(attempts) == 1 else original(*args, **kwargs)
    monkeypatch.setattr(handler, '_fail_active_job_for_hms', fail_once)
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    send(monitor, gcode_state='FAILED', gcode_file='current.3mf', hms=STOPPING_ERROR)
    assert state(db) == 'running' and attempts == ['write']
    send(monitor, retained=True, gcode_state='RUNNING', gcode_file='current.3mf')
    assert state(db) == 'running' and attempts == ['write']
    send(monitor, gcode_state='FAILED')
    assert state(db) == 'failed' and attempts == ['write', 'write']
