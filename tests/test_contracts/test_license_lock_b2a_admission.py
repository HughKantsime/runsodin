"""B2a: printer license checks and the final print-start admission fence.

Printer 9 is selected and printer 10 is locked (enterprise license, max_printers=1).
"""
import threading
import time

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from core import license_lock as policy
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_policy_b1 import policy_db, pick, status  # noqa: F401

SELECTED, LOCKED = 9, 10


def _claim(db, printer_id, token, job_id=1):
    db.execute(text("INSERT INTO printer_dispatch_claims (printer_id,job_id,physical_key,configuration_key,"
                    "payload_sha256,token,state) VALUES (:p,:j,:k,:k,:k,:t,'adapter_entered')"),
               {'p': printer_id, 'j': job_id, 't': token, 'k': '0' * 64})
    db.commit()


def _admitted(db):
    return policy._read(db, policy.ADMITTED_KEY) or {}


def _count_queries(engine):
    seen = []
    listener = lambda *a, **k: seen.append(a[2])
    event.listen(engine, 'before_cursor_execute', listener)
    return seen, lambda: event.remove(engine, 'before_cursor_execute', listener)


def test_printer_capabilities(policy_db):
    snap = policy.snapshot(policy_db)
    assert snap.enforcing and SELECTED in snap.printers and LOCKED not in snap.printers
    assert policy.printer_allowed(snap, SELECTED, 'N')
    assert not policy.printer_allowed(snap, LOCKED, 'N')
    for cap in 'MCV':  # exempt attempts arrive in B2b; members only until then
        assert not policy.printer_allowed(snap, LOCKED, cap)
    assert policy.printer_allowed(snap, LOCKED, 'R')
    with pytest.raises(ValueError):
        policy.printer_allowed(snap, SELECTED, 'X')
    with pytest.raises(policy.LicenseLockError) as exc:
        policy.require_printer(snap, LOCKED)
    assert exc.value.status_code == 403
    assert exc.value.detail['code'] == 'license_printer_locked'
    assert exc.value.detail['resource_type'] == 'printer' and exc.value.detail['resource_id'] == LOCKED
    assert exc.value.headers['X-ODIN-License-Lock'] == 'license_printer_locked'


def test_user_errors_keep_user_resource_type(policy_db):
    with pytest.raises(policy.LicenseLockError) as exc:
        policy.require_user(policy.snapshot(policy_db), 1)
    assert exc.value.detail['resource_type'] == 'user'


def test_rollout_off_makes_no_queries(policy_db, monkeypatch):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    engine = policy_db.get_bind()
    seen, stop = _count_queries(engine)
    try:
        assert policy.printer_refusal(engine, LOCKED) is None
        assert policy.admit_start(engine, printer_id=LOCKED, job_id=1, claim_token='t' * 32) is None
        from modules.printers.license_start_gate import create_license_gate
        previous = object()
        assert create_license_gate(engine, job={'id': 1, 'printer_id': LOCKED}, claim_token='t' * 32,
                                   previous=previous) is previous
        assert create_license_gate(engine, job={'id': 1, 'printer_id': LOCKED}, claim_token='t' * 32) is None
    finally:
        stop()
    assert seen == []


def test_admission_records_digest_not_token(policy_db):
    token = 'a' * 32
    _claim(policy_db, SELECTED, token, job_id=7)
    engine = policy_db.get_bind()
    assert policy.admit_start(engine, printer_id=SELECTED, job_id=7, claim_token=token) is None
    entries = _admitted(policy_db)
    assert len(entries) == 1
    (key, entry), = entries.items()
    assert token not in key and token not in str(entry)
    assert key == policy._attempt_key(token)
    assert entry['printer_id'] == SELECTED and entry['job_id'] == 7
    assert entry['revision'] == status(policy_db)['revision'] and entry['admitted_at']


def test_locked_printer_refused_and_nothing_recorded(policy_db):
    token = 'b' * 32
    _claim(policy_db, LOCKED, token)
    refused = policy.admit_start(policy_db.get_bind(), printer_id=LOCKED, job_id=1, claim_token=token)
    assert refused is not None and refused.detail['code'] == 'license_printer_locked'
    assert _admitted(policy_db) == {}
    assert 'not in the active license selection' in policy.refusal_message(refused)
    assert policy.refusal_message(refused).endswith('no command was sent')


def test_stale_admissions_are_pruned(policy_db):
    engine = policy_db.get_bind()
    _claim(policy_db, SELECTED, 'c' * 32)
    assert policy.admit_start(engine, printer_id=SELECTED, job_id=1, claim_token='c' * 32) is None
    policy_db.execute(text('DELETE FROM printer_dispatch_claims'))
    policy_db.commit()
    _claim(policy_db, SELECTED, 'd' * 32, job_id=2)
    assert policy.admit_start(engine, printer_id=SELECTED, job_id=2, claim_token='d' * 32) is None
    assert set(_admitted(policy_db)) == {policy._attempt_key('d' * 32)}


def test_policy_failure_refuses(policy_db, monkeypatch):
    def broken(db, **kwargs):
        raise RuntimeError('database gone')
    monkeypatch.setattr(policy, '_reconcile', broken)
    refused = policy.admit_start(policy_db.get_bind(), printer_id=SELECTED, job_id=1, claim_token='e' * 32)
    assert refused.status_code == 503 and refused.detail['code'] == 'license_policy_unavailable'
    assert 'could not be checked' in policy.refusal_message(refused)


def test_order_selection_first_refuses(policy_db):
    _claim(policy_db, SELECTED, 'f' * 32)
    pick(policy_db, users=[3], printers=[LOCKED])  # printer 9 is now locked
    refused = policy.admit_start(policy_db.get_bind(), printer_id=SELECTED, job_id=1, claim_token='f' * 32)
    assert refused.detail['code'] == 'license_printer_locked'
    assert _admitted(policy_db) == {}


def test_order_admission_first_is_kept(policy_db):
    _claim(policy_db, SELECTED, 'g' * 32)
    assert policy.admit_start(policy_db.get_bind(), printer_id=SELECTED, job_id=1, claim_token='g' * 32) is None
    pick(policy_db, users=[3], printers=[LOCKED])
    # The admitted attempt stays on record for B2b to keep as the exempt print.
    assert set(_admitted(policy_db)) == {policy._attempt_key('g' * 32)}


def test_admission_waits_for_an_inflight_selection_change(policy_db):
    """A selection change holding the policy lock linearizes before admission."""
    _claim(policy_db, SELECTED, 'h' * 32)
    engine = policy_db.get_bind()
    current = status(policy_db)
    changer = Session(engine)
    policy.replace_selection(changer, printers=[LOCKED], users=[3],
                             expected_revision=current['revision'], actor=3)  # holds the lock, uncommitted
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        'r', policy.admit_start(engine, printer_id=SELECTED, job_id=1, claim_token='h' * 32)))
    worker.start()
    time.sleep(0.5)
    assert worker.is_alive(), 'admission must wait for the policy lock'
    changer.commit()
    changer.close()
    worker.join(10)
    assert not worker.is_alive()
    assert result['r'] is not None and result['r'].detail['code'] == 'license_printer_locked'
    assert _admitted(policy_db) == {}


def test_early_refusal(policy_db):
    engine = policy_db.get_bind()
    assert policy.printer_refusal(engine, SELECTED) is None
    refused = policy.printer_refusal(engine, LOCKED)
    assert refused.detail['code'] == 'license_printer_locked'


# --- Gate composition ------------------------------------------------------

def _gate(policy_db, previous=None, printer_id=SELECTED, token='i' * 32):
    from modules.printers.license_start_gate import create_license_gate
    _claim(policy_db, printer_id, token)
    return create_license_gate(policy_db.get_bind(), job={'id': 1, 'printer_id': printer_id},
                               claim_token=token, previous=previous)


def _previous(result=True, prepared=True, code='printer_maintenance_hold'):
    from modules.printers.dispatch_ams_gate import record_refusal
    calls = []
    def gate():
        calls.append('call')
        return True if result else record_refusal(gate, code, 'held')
    def prepare():
        calls.append('prepare')
        return True if prepared else record_refusal(gate, code, 'held')
    gate.refusal = None
    gate.prepare = prepare
    gate.calls = calls
    return gate


def test_gate_admits_after_previous(policy_db):
    previous = _previous()
    gate = _gate(policy_db, previous)
    assert gate.prepare() is True and gate() is True
    assert previous.calls == ['prepare', 'call']
    assert set(_admitted(policy_db)) == {policy._attempt_key('i' * 32)}


def test_gate_previous_refusal_skips_admission(policy_db, monkeypatch):
    previous = _previous(result=False)
    gate = _gate(policy_db, previous)
    monkeypatch.setattr(policy, 'admit_start', lambda *a, **k: pytest.fail('admission after a refusal'))
    assert gate() is False
    assert gate.refusal.code == 'printer_maintenance_hold'


def test_gate_prepare_refusal_propagates(policy_db):
    gate = _gate(policy_db, _previous(prepared=False, code='ams_read_failed'))
    assert gate.prepare() is False and gate.refusal.code == 'ams_read_failed'


def test_gate_previous_exception_is_a_refusal(policy_db):
    def boom():
        raise OSError('socket')
    gate = _gate(policy_db, boom)
    assert gate() is False and gate.refusal.code == 'exception:OSError'


def test_gate_locked_printer_and_single_use(policy_db):
    gate = _gate(policy_db, None, printer_id=LOCKED, token='j' * 32)
    assert gate.prepare() is True
    assert gate() is False
    assert gate.refusal.code == 'license_printer_locked'
    assert 'no command was sent' not in gate.refusal.detail
    assert _admitted(policy_db) == {}


def test_gate_single_use_after_admission(policy_db):
    gate = _gate(policy_db, None)
    assert gate() is True
    assert gate() is False and gate.refusal.code == 'already_used'


# --- Transports --------------------------------------------------------------

def _refusing_gate(code='license_printer_locked'):
    from modules.printers.dispatch_ams_gate import record_refusal
    def gate():
        return record_refusal(gate, code, 'This printer is not in the active license selection (cap)')
    gate.refusal = None
    gate.prepare = lambda: True
    return gate


def test_moonraker_gate_runs_after_upload_before_start(monkeypatch):
    from modules.printers import dispatch
    from modules.printers.adapters import moonraker
    calls = []
    class Fake:
        def __init__(self, **kw): pass
        def upload_file(self, *a): calls.append('upload'); return True
        def start_print(self, *a): calls.append('start'); return True
    monkeypatch.setattr(moonraker, 'MoonrakerPrinter', Fake)
    monkeypatch.setattr(dispatch, '_ws', lambda *a, **k: None)
    ok, message = dispatch._dispatch_moonraker(1, '/x.gcode', 'x.gcode', {'ip': 'h', 'port': 1},
                                               before_start=_refusing_gate())
    assert ok is False and calls == ['upload']
    assert 'license_printer_locked' in message and 'no print command was sent' in message
    calls.clear()
    assert dispatch._dispatch_moonraker(1, '/x.gcode', 'x.gcode', {'ip': 'h', 'port': 1})[0] is True
    assert calls == ['upload', 'start']


def test_prusalink_gate_runs_before_combined_upload_and_start(monkeypatch):
    from modules.printers import dispatch
    from modules.printers.adapters import prusalink
    calls = []
    class Fake:
        def __init__(self, **kw): pass
        def upload_and_print(self, *a): calls.append('upload_and_print'); return True
    monkeypatch.setattr(prusalink, 'PrusaLinkPrinter', Fake)
    monkeypatch.setattr(dispatch, '_ws', lambda *a, **k: None)
    ok, message = dispatch._dispatch_prusalink(1, '/x.gcode', 'x.gcode', {'ip': 'h', 'port': 1},
                                               before_start=_refusing_gate())
    assert ok is False and calls == [] and 'license_printer_locked' in message
    assert dispatch._dispatch_prusalink(1, '/x.gcode', 'x.gcode', {'ip': 'h', 'port': 1})[0] is True
    assert calls == ['upload_and_print']


def test_run_admitted_dispatch_marks_license_refusal_clean(monkeypatch):
    from modules.printers import dispatch
    from modules.printers.adapters import moonraker
    class Fake:
        def __init__(self, **kw): pass
        def upload_file(self, *a): return True
        def start_print(self, *a): pytest.fail('start command sent')
    monkeypatch.setattr(moonraker, 'MoonrakerPrinter', Fake)
    monkeypatch.setattr(dispatch, '_ws', lambda *a, **k: None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: None)
    gate = _refusing_gate()
    job = {'id': 1, 'printer_id': SELECTED, 'item_name': 'part'}
    ok, _ = dispatch._run_admitted_dispatch(job, {'api_type': 'moonraker', 'ip': 'h', 'port': 1},
                                            '/x.gcode', 'x.gcode', {}, None, None, before_start=gate)
    assert ok is False and gate.clean_no_start is True


# --- Dispatch entry, scheduler and start-next -------------------------------

def test_dispatch_prepared_refuses_locked_printer_before_any_lock(policy_db, monkeypatch):
    from modules.printers import dispatch_orchestration as orchestration
    from modules.printers import dispatch
    monkeypatch.setattr(orchestration, 'acquire_target_lock', lambda *a, **k: pytest.fail('target lock taken'))
    notified = []
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda job, reason, **k: notified.append((reason, k)))
    ok, message = orchestration.dispatch_prepared({'id': 1, 'printer_id': LOCKED}, {'api_type': 'moonraker'},
                                                  '/x', 'x', 's', None)
    assert ok is False and 'not in the active license selection' in message
    assert notified and notified[0][1]['gate_refusal'].code == 'license_printer_locked'
    assert notified[0][1]['message'] == message


def test_start_next_refuses_locked_printer(policy_db):
    from types import SimpleNamespace
    from uuid import uuid4
    from modules.printers import start_next
    data = SimpleNamespace(request_id=uuid4(), suggested_job_id=1, bed_clear_confirmed=True,
                           suggestion_fingerprint='f', attestation=None)
    user = {'id': 3, 'role': 'admin'}
    result = start_next.start(policy_db, SimpleNamespace(id=LOCKED), user, data)
    assert result['state'] == 'refused' and result['code'] == 'license_printer_locked'


def test_scheduler_uses_only_selected_printers(policy_db):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    printers = [r[0] for r in policy_db.execute(text('SELECT id FROM printers WHERE is_active IS TRUE'))]
    assert set(printers) >= {SELECTED, LOCKED}
    captured = {}
    original = Scheduler._find_best_fit
    def spy(self, job, printer_states, *a, **k):
        captured['printers'] = set(printer_states)
        return original(self, job, printer_states, *a, **k)
    import modules.jobs.scheduler as scheduler_module
    scheduler_module.Scheduler._find_best_fit = spy
    try:
        policy_db.execute(text("INSERT INTO jobs (item_name,status,priority,hold,duration_hours,quantity,created_at,updated_at) "
                               "VALUES ('license part','pending',3,FALSE,1.0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        policy_db.commit()
        Scheduler(SchedulerConfig()).run(policy_db)
    finally:
        scheduler_module.Scheduler._find_best_fit = original
    assert captured.get('printers') is not None
    assert LOCKED not in captured['printers'] and SELECTED in captured['printers']
