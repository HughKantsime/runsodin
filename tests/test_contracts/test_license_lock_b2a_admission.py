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


def test_gate_admits_before_safety_checks_and_withdraws_on_refusal(policy_db, monkeypatch):
    order = []
    real_admit = policy.admit_start
    monkeypatch.setattr(policy, 'admit_start', lambda *a, **k: order.append('license') or real_admit(*a, **k))
    def tracked():
        order.append('safety')
        return False  # refuses without recording a code
    tracked.refusal = None
    tracked.prepare = lambda: True
    gate = _gate(policy_db, tracked)
    assert gate() is False
    # License first, so waiting for its lock never ages the safety checks' freshness.
    assert order == ['license', 'safety']
    assert gate.refusal.code == 'gate_rejected'
    assert _admitted(policy_db) == {}  # withdrawn: this attempt will not be sent


def test_gate_previous_refusal_code_is_kept(policy_db):
    gate = _gate(policy_db, _previous(result=False))
    assert gate() is False and gate.refusal.code == 'printer_maintenance_hold'
    assert _admitted(policy_db) == {}


def test_locked_printer_never_runs_safety_checks(policy_db):
    previous = _previous()
    gate = _gate(policy_db, previous, printer_id=LOCKED, token='l' * 32)
    assert gate() is False and previous.calls == []


def test_gate_prepare_refusal_propagates(policy_db):
    gate = _gate(policy_db, _previous(prepared=False, code='ams_read_failed'))
    assert gate.prepare() is False and gate.refusal.code == 'ams_read_failed'


def test_gate_previous_exception_is_a_refusal(policy_db):
    def boom():
        raise OSError('socket')
    gate = _gate(policy_db, boom)
    assert gate() is False and gate.refusal.code == 'exception:OSError'
    assert _admitted(policy_db) == {}


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


def _insert_job(db, name):
    # Org 1 owns both fixture printers.
    db.execute(text("INSERT INTO jobs (charged_to_org_id,item_name,status,priority,hold,duration_hours,quantity,"
                    "created_at,updated_at) VALUES (1,:name,'pending',3,FALSE,1.0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
               {'name': name})


def _only_printer(original, printer_id):
    def spy(self, job, *a, **k):
        if 'printer_states' in k:
            k['printer_states'] = {printer_id: k['printer_states'][printer_id]}
        else:
            a = ({printer_id: a[0][printer_id]},) + a[1:]
        return original(self, job, *a, **k)
    return spy


# --- Review round 1 fixes --------------------------------------------------

def test_sqlite_admission_wait_is_bounded_and_restored(policy_db):
    engine = policy_db.get_bind()
    with engine.connect() as c:
        original = c.exec_driver_sql('PRAGMA busy_timeout').scalar()
    assert original > policy.ADMISSION_WAIT_MS
    holder = Session(engine)
    policy._lock(holder)  # another writer holds the policy lock
    try:
        started = time.monotonic()
        refused = policy.admit_start(engine, printer_id=SELECTED, job_id=1, claim_token='m' * 32)
        waited = time.monotonic() - started
    finally:
        holder.rollback(); holder.close()
    assert refused is not None and refused.detail['code'] == 'license_policy_unavailable'
    assert 1.5 <= waited < original / 1000
    for _ in range(3):
        with engine.connect() as c:
            assert c.exec_driver_sql('PRAGMA busy_timeout').scalar() == original


def test_reconcile_prunes_admissions_for_released_claims(policy_db):
    _claim(policy_db, SELECTED, 'n' * 32)
    assert policy.admit_start(policy_db.get_bind(), printer_id=SELECTED, job_id=1, claim_token='n' * 32) is None
    policy_db.execute(text('DELETE FROM printer_dispatch_claims'))
    policy_db.commit()
    policy.reconcile(policy_db); policy_db.commit()
    assert _admitted(policy_db) == {}


def test_locked_snapshot_executors(policy_db):
    engine = policy_db.get_bind()
    with Session(engine) as db:
        snap = policy.locked_snapshot(db)
        assert snap is not None and policy.printer_allowed(snap, SELECTED)
        assert not policy.printer_allowed(snap, LOCKED)
        db.rollback()
    with engine.connect() as conn:
        assert policy.locked_snapshot(conn) is None  # Session only; fail closed
    raw = engine.raw_connection()
    try:
        assert policy.locked_snapshot(raw) is None
        # DBAPI executors (monitor transactions) are read with a plain cursor query.
        assert policy.printer_allowed_now(raw, SELECTED) and not policy.printer_allowed_now(raw, LOCKED)
    finally:
        raw.close()
    with engine.connect() as conn:
        assert policy.printer_allowed_now(conn, SELECTED) and not policy.printer_allowed_now(conn, LOCKED)


def test_sqlite_restore_targets_the_same_pooled_connection(policy_db):
    engine = policy_db.get_bind()
    with engine.connect() as c:
        original = c.exec_driver_sql('PRAGMA busy_timeout').scalar()
    other = engine.connect()  # keep a second pooled connection checked out
    try:
        _claim(policy_db, SELECTED, 'p' * 32)
        assert policy.admit_start(engine, printer_id=SELECTED, job_id=1, claim_token='p' * 32) is None
    finally:
        other.close()
    held = [engine.connect() for _ in range(3)]
    try:
        assert [c.exec_driver_sql('PRAGMA busy_timeout').scalar() for c in held] == [original] * 3
    finally:
        for c in held:
            c.close()


def test_locked_snapshot_holds_the_lock_until_caller_commits(policy_db):
    engine = policy_db.get_bind()
    current = status(policy_db)
    caller = Session(engine)
    assert policy.printer_allowed(policy.locked_snapshot(caller), SELECTED)
    done = {}
    def change():
        with Session(engine) as db:
            policy.replace_selection(db, printers=[LOCKED], users=[3], expected_revision=current['revision'], actor=3)
            db.commit()
            done['at'] = time.monotonic()
    worker = threading.Thread(target=change)
    worker.start()
    time.sleep(0.5)
    assert worker.is_alive()
    released = time.monotonic()
    caller.commit(); caller.close()
    worker.join(10)
    assert done['at'] >= released


def test_scheduler_rechecks_under_lock_before_assignment(policy_db, monkeypatch):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    import modules.jobs.scheduler as scheduler_module
    stale = policy.stored_snapshot(policy_db)  # printer 9 selected
    pick(policy_db, users=[3], printers=[LOCKED])  # committed: printer 9 is now locked
    # The early filter still sees the stale selection that includes printer 9 ...
    monkeypatch.setattr(policy, 'stored_snapshot', lambda db: stale)
    monkeypatch.setattr(scheduler_module.Scheduler, '_find_best_fit', _only_printer(Scheduler._find_best_fit, SELECTED))
    _insert_job(policy_db, 'race part')
    policy_db.commit()
    job_id = policy_db.execute(text("SELECT id FROM jobs WHERE item_name='race part'")).scalar()
    attempts = []
    real_once = Scheduler._run_once
    monkeypatch.setattr(scheduler_module.Scheduler, '_run_once',
                        lambda self, db, start=None: attempts.append(1) or real_once(self, db, start))
    result = Scheduler(SchedulerConfig()).run(policy_db)
    # ... but the fresh end-of-run check under the lock discards the run (and the retry).
    assert len(attempts) == 2 and result.scheduled_count == 0 and result.success is False
    row = policy_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=:id'), {'id': job_id}).mappings().one()
    assert row['status'] == 'pending' and row['printer_id'] is None
    assert any('license selection' in e for e in result.errors), result.errors


def test_education_services_refuse_locked_printer(policy_db, monkeypatch):
    from modules.organizations import education_policy, education_dispatch_policy, education_training_policy
    monkeypatch.setattr(education_training_policy, 'job_training_allowed', lambda *a, **k: True)
    monkeypatch.setattr(education_policy, 'scheduler_context', lambda *a, **k: pytest.fail('past license check'))
    assert education_policy.advance_schedule(policy_db, submission_id=1, job_id=1, printer_id=LOCKED,
                                             expected_revision=1) is False
    monkeypatch.setattr(education_dispatch_policy.owner, '_ExecutionAdapter',
                        lambda *a: pytest.fail('reservation attempted'))
    extension = sorted(education_dispatch_policy.owner._ALLOWED_REMOTE_EXTENSIONS)[0]
    assert education_dispatch_policy.reserve_dispatch(policy_db, job_id=1, printer_id=LOCKED,
                                                      expected_revision=1, extension=extension) is None


def test_bambu_v2_license_refusal_is_a_clean_no_start(policy_db, monkeypatch):
    from types import SimpleNamespace as NS
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import ftp_upload, session, broker_policy
    commands = []
    def run_command(config, method, *a, before_command=None, **k):
        if before_command is not None and before_command() is not True:
            return False
        commands.append(method)
        return True
    monkeypatch.setattr(dispatch, '_ws', lambda *a, **k: None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **k: None)
    monkeypatch.setattr(ftp_upload, 'upload_file', lambda *a: True)
    monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config', lambda *a, **k:
                        NS(host='localhost', port=8883, use_tls=True))
    monkeypatch.setattr(session, 'run_command', run_command)
    monkeypatch.setattr(dispatch, '_dispatch_bambu', dispatch._dispatch_bambu_v2)
    gate = _gate(policy_db, None, printer_id=LOCKED, token='o' * 32)
    creds = {'api_type': 'bambu', 'ip': 'localhost', 'serial': 's', 'access_code': 'a'}
    ok, message = dispatch._run_admitted_dispatch({'id': 1, 'printer_id': LOCKED, 'item_name': 'part'}, creds,
                                                  '', 'inert.3mf', {}, None, None, before_start=gate)
    assert ok is False and commands == []
    assert 'license_printer_locked' in message and 'reconciliation' not in message
    assert gate.clean_no_start is True


def test_scheduler_control_assigns_selected_printer(policy_db, monkeypatch):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    import modules.jobs.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module.Scheduler, '_find_best_fit', _only_printer(Scheduler._find_best_fit, SELECTED))
    _insert_job(policy_db, 'race part')
    policy_db.commit()
    assert Scheduler(SchedulerConfig()).run(policy_db).scheduled_count == 1


# --- Review round 3 fixes --------------------------------------------------

def test_early_check_never_blocks_on_the_callers_writer_lock(policy_db):
    engine = policy_db.get_bind()
    caller = Session(engine)
    caller.execute(text('BEGIN IMMEDIATE'))
    caller.execute(text("INSERT INTO system_config (key,value) VALUES ('b2a-probe','1')"))
    policy._cache.clear()  # cold cache: a reconciling check would need the writer lock
    try:
        started = time.monotonic()
        assert policy.printer_allowed_now(caller, SELECTED) is True
        assert policy.printer_allowed_now(caller, LOCKED) is False
        assert time.monotonic() - started < 0.5
    finally:
        caller.rollback(); caller.close()


def _education_only_run(policy_db, monkeypatch, advance):
    """One Education job approved for printer 9; no generic jobs, so nothing writes
    before the Education transition (the case where pysqlite would otherwise commit
    a released savepoint immediately)."""
    from types import SimpleNamespace as NS
    from modules.jobs.scheduler import Scheduler
    import modules.jobs.scheduler as scheduler_module
    from core.registry import registry
    provider = NS(scheduler_context=lambda db, job_id: {
                      'id': 1, 'lifecycle_revision': 1, 'org_id': 1, 'print_file_id': 1,
                      'approved_printer_id': SELECTED, 'authorized': True, 'status': 'pending'},
                  advance_schedule=advance,
                  reset_stale_schedule=lambda *a, **k: False,
                  reconcile_schedule_denial=lambda *a, **k: False)
    real_get = registry.get_provider
    monkeypatch.setattr(registry, 'get_provider',
                        lambda name: provider if name == 'EducationPolicyProvider' else real_get(name))
    monkeypatch.setattr(scheduler_module.Scheduler, '_is_education_job', staticmethod(lambda db, job_id: True))
    monkeypatch.setattr(scheduler_module, 'evaluate_submission_compatibility', lambda *a, **k: {'compatible': True})
    monkeypatch.setattr(scheduler_module.Scheduler, '_find_best_fit', _only_printer(Scheduler._find_best_fit, SELECTED))
    _insert_job(policy_db, 'education part')
    policy_db.commit()


def test_discarded_run_undoes_education_savepoint_writes(policy_db, monkeypatch):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    stale = policy.stored_snapshot(policy_db)
    pick(policy_db, users=[3], printers=[LOCKED])  # printer 9 now locked
    monkeypatch.setattr(policy, 'stored_snapshot', lambda db: stale)  # early filter still sees 9
    def advance(db, **k):
        with db.begin_nested():  # Education records its transition in a savepoint
            db.execute(text("INSERT INTO system_config (key,value) VALUES ('b2a-nested-'||abs(random()),'1')"))
        return True
    _education_only_run(policy_db, monkeypatch, advance)
    result = Scheduler(SchedulerConfig()).run(policy_db)
    assert result.scheduled_count == 0 and result.success is False
    leftover = policy_db.execute(text("SELECT count(*) FROM system_config WHERE key LIKE 'b2a-nested-%'")).scalar()
    assert leftover == 0


def test_planning_holds_no_writer_lock_and_policy_is_locked_last(policy_db, monkeypatch):
    """Read-only planning keeps no SQLite writer lock, and the policy row is touched
    (reconciled/locked) only by the final check, even with a cold cache."""
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    import modules.jobs.scheduler as scheduler_module
    engine = policy_db.get_bind()
    order = []
    real_reconcile = policy._reconcile
    monkeypatch.setattr(policy, '_reconcile', lambda db, **k: order.append('policy') or real_reconcile(db, **k))
    def advance(db, **k):
        order.append('education_write')
        return True
    _education_only_run(policy_db, monkeypatch, advance)
    real_fit = scheduler_module.Scheduler._find_best_fit
    def fit_then_probe(self, job, *a, **k):
        order.append('planning')
        other = engine.raw_connection()
        try:  # another writer commits while this run is still planning
            other.execute('PRAGMA busy_timeout=200')
            other.execute("INSERT INTO system_config (key,value) VALUES ('b2a-planning-probe','1')")
            other.commit()
        finally:
            other.close()
        return real_fit(self, job, *a, **k)
    monkeypatch.setattr(scheduler_module.Scheduler, '_find_best_fit', fit_then_probe)
    policy._cache.clear()
    result = Scheduler(SchedulerConfig()).run(policy_db)
    assert result.scheduled_count == 1
    assert policy_db.execute(text("SELECT count(*) FROM system_config WHERE key='b2a-planning-probe'")).scalar() == 1
    assert order.count('policy') >= 1
    first_policy = order.index('policy')
    assert first_policy > order.index('education_write') and first_policy > order.index('planning')


def test_run_starts_from_a_clean_transaction_boundary(policy_db):
    """A lock or pending write the caller holds is committed before the run locks jobs."""
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    policy._lock(policy_db)  # caller holds the policy row
    policy_db.execute(text("INSERT INTO system_config (key,value) VALUES ('b2a-caller-pending','1')"))
    Scheduler(SchedulerConfig()).run(policy_db)
    with Session(policy_db.get_bind()) as other:
        assert other.execute(text("SELECT count(*) FROM system_config WHERE key='b2a-caller-pending'")).scalar() == 1


def test_license_recheck_runs_after_alert_row_locks(policy_db, monkeypatch):
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    import modules.jobs.scheduler as scheduler_module
    from modules.notifications import alert_dispatcher
    order = []
    real_locked = policy.locked_snapshot
    monkeypatch.setattr(policy, 'locked_snapshot', lambda db: order.append('license') or real_locked(db))
    monkeypatch.setattr(alert_dispatcher, 'lock_queue_alert', lambda *a, **k: order.append('alert') or False)
    original = _only_printer(Scheduler._find_best_fit, SELECTED)
    def fit(self, job, *a, **k):
        return None if job.item_name == 'skip part' else original(self, job, *a, **k)
    monkeypatch.setattr(scheduler_module.Scheduler, '_find_best_fit', fit)
    _insert_job(policy_db, 'fit part')
    _insert_job(policy_db, 'skip part')
    policy_db.commit()
    result = Scheduler(SchedulerConfig()).run(policy_db)
    assert result.scheduled_count == 1
    assert 'alert' in order and order[-1] == 'license' and order.index('license') > order.index('alert')
