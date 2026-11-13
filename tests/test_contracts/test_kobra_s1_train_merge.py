"""Local integration checks for the S1 merge onto the gated 2.2 train."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_kobra_s1_gcode_pipeline import s1, ONE_COLOUR, _schedule, _start_next
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('state,target,filename,confirmed', [
    ('complete', 210, 'attempt.gcode', False),
    ('printing', 0, 'attempt.gcode', False),
    ('printing', 210, 'other.gcode', False),
    ('printing', 210, 'attempt.gcode', True),
    ('HEATING', 210, 'attempt.gcode', True),
    ('LEVELING', 210, 'attempt.gcode', True),
    ('unknown', 210, 'attempt.gcode', False),
])
def test_start_confirmation_requires_correlated_activity_and_heat(monkeypatch, state, target, filename, confirmed):
    from modules.printers import s1_dispatch
    clock = [0]
    monkeypatch.setattr(s1_dispatch.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(s1_dispatch.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    printer = SimpleNamespace(get_status=lambda: SimpleNamespace(raw_print_state=state, nozzle_target=target, filename=filename))
    assert s1_dispatch._confirm_started(printer, 'attempt.gcode', deadline=90) is confirmed
    assert clock[0] == (0 if confirmed else 90)


def test_late_status_cannot_confirm_outside_the_publish_window(monkeypatch):
    from modules.printers import s1_dispatch
    monkeypatch.setattr(s1_dispatch.time, 'monotonic', lambda: 91)
    printer = SimpleNamespace(get_status=lambda: SimpleNamespace(raw_print_state='printing', nozzle_target=210, filename='attempt.gcode'))
    assert not s1_dispatch._confirm_started(printer, 'attempt.gcode', deadline=90)


def test_s1_uses_the_train_license_wrapper_once_after_upload(s1, monkeypatch):
    from core import license_lock
    calls = []
    monkeypatch.setattr(license_lock, 'enabled', lambda: True)
    # Exercise the real composed license gate; substitute only policy I/O.
    monkeypatch.setattr(license_lock, 'printer_refusal', lambda *a, **k: None)
    monkeypatch.setattr(license_lock, 'require_printer', lambda *a, **k: None)
    def admit(*a, **kw):
        assert s1.device.files and not s1.device.publishes
        calls.append(kw)
    monkeypatch.setattr(license_lock, 'admit_start', admit)
    uploaded = s1.upload(ONE_COLOUR)
    assert _schedule(s1, uploaded, 'print-files', {'ams_mapping': [{'source_slot': 1, 'target_slot': 2}]}).status_code == 200
    _, started = _start_next(s1)
    assert started.status_code == 200, started.text
    assert started.json()['state'] == 'dispatched', started.json()
    assert len(calls) == len(s1.device.publishes) == 1


def test_s1_held_release_repeat_and_vector_edit_read_the_gcode(s1):
    from sqlalchemy.orm import Session
    from modules.jobs.models import Job
    from modules.jobs.ams_mapping import assert_release_ready, checked_repeat_mapping, checked_changes
    from fastapi import HTTPException
    uploaded = s1.upload(ONE_COLOUR)
    response = _schedule(s1, uploaded, 'print-files', {'hold': True, 'ams_mapping': [{'source_slot': 1, 'target_slot': 2}]})
    assert response.status_code == 200, response.text
    with Session(s1.engine) as db:
        job = db.get(Job, response.json()['job_id'])
        actor = {'id': 1, 'role': 'admin', 'group_id': 1}
        assert_release_ready(db, actor, job)
        assert checked_repeat_mapping(db, actor, job) == ([1], True)
        assert checked_changes(db, actor, job, {'ams_mapping': [1]})['ams_mapping'] == [1]
        db.execute(text("UPDATE filament_slots SET do_not_feed=true WHERE printer_id=59 AND slot_number=2"))
        with pytest.raises(HTTPException, match='slot_do_not_feed'):
            assert_release_ready(db, actor, job)
        with pytest.raises(HTTPException, match='slot_do_not_feed'):
            checked_repeat_mapping(db, actor, job)
        assert job.hold is True and s1.device.publishes == []


def test_s1_unattributed_audit_shares_the_monitor_transaction(s1):
    from modules.printers.monitors.moonraker_job_tracking import MoonrakerJobTracking
    from core.audit_integrity import verify
    uploaded = s1.upload(ONE_COLOUR)
    response = _schedule(s1, uploaded, 'print-files', {})
    assert response.status_code == 200, response.text
    job_id = response.json()['job_id']
    monitor = MoonrakerJobTracking()
    monitor.printer_id, monitor.name = 59, 'Synthetic S1'
    with s1.engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament.unattributed'")).scalar_one()
        conn.rollback()
        transaction = conn.begin()
        monitor._deduct_s1_filament(conn, job_id, observed_grams=10)
        assert conn.in_transaction()
        assert conn.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament.unattributed'")).scalar_one() == before + 1
        assert verify(conn)['ok']
        transaction.rollback()
        assert conn.execute(text("SELECT count(*) FROM audit_logs WHERE action='filament.unattributed'")).scalar_one() == before


def test_s1_license_refusal_after_upload_sends_no_publish(s1, monkeypatch):
    from core import license_lock
    monkeypatch.setattr(license_lock, 'enabled', lambda: True)
    monkeypatch.setattr(license_lock, 'printer_refusal', lambda *a, **k: None)
    monkeypatch.setattr(license_lock, 'require_printer', lambda *a, **k: None)
    def refuse(*a, **kw):
        assert s1.device.files and not s1.device.publishes
        return SimpleNamespace(detail={'code': 'license_printer_locked'})
    monkeypatch.setattr(license_lock, 'admit_start', refuse)
    monkeypatch.setattr(license_lock, 'refusal_message', lambda *a, **k: 'Printer is license locked')
    uploaded = s1.upload(ONE_COLOUR)
    scheduled = _schedule(s1, uploaded, 'print-files', {'ams_mapping': [{'source_slot': 1, 'target_slot': 2}]})
    assert scheduled.status_code == 200
    _, started = _start_next(s1)
    assert started.json()['state'] != 'dispatched', started.json()
    assert s1.device.publishes == []
    with s1.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': scheduled.json()['job_id']}).scalar_one() != 'printing'


@pytest.mark.parametrize('state', ['leveling', 'heating', 'OnPause'])
def test_start_confirmation_reads_s1_states_from_the_real_adapter(monkeypatch, state):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from modules.printers import s1_dispatch
    printer = MoonrakerPrinter('192.0.2.1', model='Kobra S1')
    monkeypatch.setattr(printer, '_get', lambda *a, **k: {'result': {'status': {
        'print_stats': {'state': state, 'filename': '/useremain/app/gk/gcodes/attempt.gcode'},
        'extruder': {'target': 210}, 'webhooks': {'state': 'ready'}}}})
    assert printer.get_status().raw_print_state == 'unknown'
    assert s1_dispatch._confirm_started(printer, 'attempt.gcode')
