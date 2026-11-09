"""B2b-1: the one exempt attempt on a newly locked printer.

Printer 9 starts selected; selecting printer 10 instead locks 9 (the transition).
Printer 10 starts locked, so it never gains an exemption.
"""
from datetime import datetime, timedelta, timezone
import time

import pytest
from sqlalchemy import text

from core import license_lock as policy
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_policy_b1 import policy_db, pick, status  # noqa: F401

NINE, TEN = 9, 10


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _attempt(db, printer_id, *, started_ago=600, received_ago=5):
    from core.economic_source_creation import _create_attempt
    now = _utcnow()
    db.execute(text('SELECT 1'))  # the factory requires a caller-owned transaction
    _create_attempt(db, {'printer_id': printer_id, 'status': 'running', 'filename': 'part.3mf',
                         'started_at': now - timedelta(seconds=started_ago),
                         'observed_received_at': now - timedelta(seconds=received_ago)})
    db.commit()
    return db.execute(text("SELECT id,economic_birth_nonce FROM print_jobs WHERE printer_id=:p "
                           "ORDER BY id DESC LIMIT 1"), {'p': printer_id}).mappings().one()


def _telemetry(db, printer_id, state='RUNNING', seen_ago=5):
    db.execute(text('UPDATE printers SET gcode_state=:s, last_seen=:t WHERE id=:p'),
               {'s': state, 't': _utcnow() - timedelta(seconds=seen_ago), 'p': printer_id})
    db.commit()


def _record(db, printer_id):
    policy._cache.clear()
    return policy.exempt_attempt(policy.snapshot(db), printer_id)


def _lock_nine(db):
    pick(db, users=[3], printers=[TEN])


def _reconcile(db):
    p = policy.reconcile(db)
    db.commit()
    policy._cache.clear()
    return p


def test_fresh_running_print_is_captured(policy_db):
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']
    assert record['nonce'] == row['economic_birth_nonce']
    snap = policy.snapshot(policy_db)
    assert policy.printer_allowed(snap, NINE, 'M')
    assert not policy.printer_allowed(snap, NINE, 'N')  # an exemption never grants new work
    assert not policy.printer_allowed(snap, NINE, 'C')  # pure check: C/V need attempt_allowed
    assert policy.attempt_allowed(policy_db, snap, NINE, 'C')
    assert policy.attempt_allowed(policy_db, snap, NINE, 'V')
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'N')


def test_paused_print_is_captured(policy_db):
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE, state='PAUSE')
    _lock_nine(policy_db)
    assert _record(policy_db, NINE)['kind'] == 'observed'


def test_stale_telemetry_is_pending_and_grants_only_bounded_monitoring(policy_db):
    row = _attempt(policy_db, NINE, received_ago=600)
    _telemetry(policy_db, NINE, seen_ago=600)
    _lock_nine(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'pending'
    assert [dict(c) for c in record['candidates']] == [{'id': row['id'], 'nonce': row['economic_birth_nonce']}]
    snap = policy.snapshot(policy_db)
    assert policy.printer_allowed(snap, NINE, 'M')
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'C')
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'V')


def test_pending_expires_after_its_window(policy_db, monkeypatch):
    _attempt(policy_db, NINE, received_ago=600)
    _telemetry(policy_db, NINE, seen_ago=600)
    _lock_nine(policy_db)
    snap = policy.snapshot(policy_db)
    record = policy.exempt_attempt(snap, NINE)
    later = policy._utc(record['until']) + timedelta(seconds=1)
    from license_manager import license_clock
    clock = license_clock()
    monkeypatch.setattr(type(clock), 'sample', lambda self, **k: (later, False))
    assert not policy.printer_allowed(snap, NINE, 'M')


def test_pending_resolves_when_fresh_telemetry_returns(policy_db):
    row = _attempt(policy_db, NINE, received_ago=600)
    _telemetry(policy_db, NINE, seen_ago=600)
    _lock_nine(policy_db)
    assert _record(policy_db, NINE)['kind'] == 'pending'
    policy_db.execute(text('UPDATE print_jobs SET observed_received_at=:t WHERE id=:id'),
                      {'t': _utcnow(), 'id': row['id']})
    policy_db.commit()
    _telemetry(policy_db, NINE)
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']


def test_two_running_rows_are_pending_without_a_row(policy_db):
    _attempt(policy_db, NINE, started_ago=900)
    _attempt(policy_db, NINE, started_ago=600)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'pending' and len(record['candidates']) == 2


def test_idle_printer_is_fully_locked(policy_db):
    _telemetry(policy_db, NINE, state='IDLE')
    _lock_nine(policy_db)
    assert _record(policy_db, NINE) is None
    snap = policy.snapshot(policy_db)
    assert not policy.printer_allowed(snap, NINE, 'M')
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'V')


def test_print_started_after_cutoff_gets_nothing(policy_db):
    row = _attempt(policy_db, NINE, started_ago=5)
    _telemetry(policy_db, NINE)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    now = policy._db_now(policy_db)
    assert policy._capture(policy_db, NINE, cutoff, now, cutoff) is None
    assert row['id']  # the running row exists; it simply started after the cutoff


def test_already_locked_printer_never_gains_an_exemption(policy_db):
    _attempt(policy_db, TEN)
    _telemetry(policy_db, TEN)
    _reconcile(policy_db)
    assert _record(policy_db, TEN) is None


def test_relicensing_drops_the_record(policy_db):
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    assert _record(policy_db, NINE) is not None
    pick(policy_db, users=[3], printers=[NINE])
    assert _record(policy_db, NINE) is None
    assert policy.printer_allowed(policy.snapshot(policy_db), NINE, 'N')


def test_terminal_status_retires_and_never_renews(policy_db):
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    before = status(policy_db)['revision']
    policy_db.execute(text("UPDATE print_jobs SET status='completed', ended_at=:t WHERE id=:id"),
                      {'t': _utcnow(), 'id': row['id']})
    policy_db.commit()
    p = _reconcile(policy_db)
    # The attempt ended: only its completion ticket remains (no M/C/V).
    assert p['exempt_attempts'][str(NINE)]['kind'] == 'completion' and p['revision'] > before
    snap = policy.snapshot(policy_db)
    assert not policy.printer_allowed(snap, NINE, 'M')
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'V')
    _attempt(policy_db, NINE, started_ago=10)  # a later print on the locked printer
    _reconcile(policy_db)
    assert _record(policy_db, NINE) is None  # the ticket ends; the new print gets nothing


def test_newer_attempt_retires_the_exemption(policy_db):
    _attempt(policy_db, NINE, started_ago=900)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    assert _record(policy_db, NINE)['kind'] == 'observed'
    _attempt(policy_db, NINE, started_ago=10)
    _reconcile(policy_db)
    assert _record(policy_db, NINE) is None


def test_disconnect_alone_does_not_retire(policy_db):
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    _telemetry(policy_db, NINE, seen_ago=3600)  # printer went offline mid-print
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed'
    snap = policy.snapshot(policy_db)
    assert policy.printer_allowed(snap, NINE, 'M')
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'C')  # C waits for fresh matching evidence


def test_attempt_allowed_rejects_a_second_running_row(policy_db):
    _attempt(policy_db, NINE, started_ago=900)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    snap = policy.snapshot(policy_db)
    assert policy.attempt_allowed(policy_db, snap, NINE, 'V')
    _attempt(policy_db, NINE, started_ago=950)  # older stray row: same attempt still newest
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'V')


# --- ODIN dispatch admitted before the cutoff --------------------------------

def _claim_and_admit(db, printer_id, token, job_id=1):
    db.execute(text("INSERT INTO printer_dispatch_claims (printer_id,job_id,physical_key,configuration_key,"
                    "payload_sha256,token,state) VALUES (:p,:j,:k,:k,:k,:t,'adapter_entered')"),
               {'p': printer_id, 'j': job_id, 't': token, 'k': '0' * 64})
    db.commit()
    assert policy.admit_start(db.get_bind(), printer_id=printer_id, job_id=job_id, claim_token=token) is None


def _bind_receipt(db, printer_id, token, print_job_id, job_id=1):
    db.execute(text("INSERT INTO dispatch_attempt_receipts (claim_token,printer_id,job_id,configuration_key,"
                    "prepared_power_key,observed_filename_key,observed_print_job_id) "
                    "VALUES (:t,:p,:j,:k,:k,:k,:o)"),
               {'t': token, 'p': printer_id, 'j': job_id, 'k': '0' * 64, 'o': print_job_id})
    db.commit()


def test_admitted_start_is_captured_then_bound_then_retired(policy_db):
    token = 'r' * 32
    _claim_and_admit(policy_db, NINE, token)
    _lock_nine(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'admitted' and token not in str(record)
    assert policy.printer_allowed(policy.snapshot(policy_db), NINE, 'M')
    row = _attempt(policy_db, NINE, started_ago=5)  # the dispatched print starts after the cutoff
    _telemetry(policy_db, NINE)
    _bind_receipt(policy_db, NINE, token, row['id'])
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']
    policy_db.execute(text("UPDATE print_jobs SET status='completed', ended_at=:t WHERE id=:id"),
                      {'t': _utcnow(), 'id': row['id']})
    policy_db.execute(text('DELETE FROM printer_dispatch_claims'))
    policy_db.commit()
    _reconcile(policy_db)
    assert _record(policy_db, NINE)['kind'] == 'completion'


def test_admitted_start_released_unsent_is_retired(policy_db):
    _claim_and_admit(policy_db, NINE, 's' * 32)
    _lock_nine(policy_db)
    assert _record(policy_db, NINE)['kind'] == 'admitted'
    policy_db.execute(text('DELETE FROM printer_dispatch_claims'))  # release_unstarted
    policy_db.commit()
    _reconcile(policy_db)
    assert _record(policy_db, NINE) is None


def test_admitted_and_already_bound_is_observed_at_capture(policy_db):
    token = 'u' * 32
    _claim_and_admit(policy_db, NINE, token)
    row = _attempt(policy_db, NINE, started_ago=30)
    _telemetry(policy_db, NINE)
    _bind_receipt(policy_db, NINE, token, row['id'])
    _lock_nine(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']


# --- Cache bound ---------------------------------------------------------------

def test_snapshot_cache_is_bounded_while_an_exemption_exists(policy_db, monkeypatch):
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    policy._cache.clear()
    policy.snapshot(policy_db)
    cached = next(iter(policy._cache.values()))
    p = policy._read(policy_db, policy.POLICY_KEY)
    now = cached[4]
    assert policy._cache_current(cached, p, now)
    real = time.monotonic
    monkeypatch.setattr(policy.time, 'monotonic', lambda: real() + policy.UNVERIFIED_CACHE_SECONDS + 1)
    assert not policy._cache_current(cached, p, now)


# --- Review round 1 ------------------------------------------------------------

def test_long_running_print_is_fresh_from_printer_telemetry(policy_db):
    """observed_received_at is written once at start; it is not the heartbeat."""
    row = _attempt(policy_db, NINE, started_ago=4 * 3600, received_ago=4 * 3600)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']
    assert policy.attempt_allowed(policy_db, policy.snapshot(policy_db), NINE, 'C')


def test_reconstructed_policy_captures_nothing(policy_db):
    _attempt(policy_db, TEN)
    _telemetry(policy_db, TEN)
    policy_db.execute(text("UPDATE system_config SET value='\"corrupt\"' WHERE key=:k"), {'k': policy.POLICY_KEY})
    policy_db.commit()
    p = _reconcile(policy_db)
    assert p['warning'] == 'license_policy_reconstructed'
    assert str(TEN) not in p['exempt_attempts']


def test_first_policy_captures_running_prints(policy_db):
    """No policy row ever existed: nothing was locked before, so running prints are kept."""
    row = _attempt(policy_db, TEN)
    _telemetry(policy_db, TEN)
    old = None
    resolved = {'printers': [NINE]}
    entitlement = dict(policy._read(policy_db, policy.POLICY_KEY)['entitlement'])
    result = policy._exemptions(policy_db, old, resolved, entitlement, reconstructed=False)
    assert result[str(TEN)]['print_job_id'] == row['id']
    assert policy._exemptions(policy_db, old, resolved, entitlement, reconstructed=True) == {}


def test_expiry_cutoff_is_the_deadline_only_for_the_expiry_itself(policy_db, monkeypatch):
    row = _attempt(policy_db, NINE, started_ago=60)  # started after the (past) deadline
    _telemetry(policy_db, NINE)
    stored = policy._read(policy_db, policy.POLICY_KEY)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    import core.license_clock as lc
    monkeypatch.setattr(lc, 'instant', lambda v: datetime.fromisoformat(v))
    expired = dict(stored['entitlement'], state='verified_expired', enforce=True, deadline=past)
    # The expiry transition itself: a valid (already enforcing) license turns expired.
    # A print started after the deadline gets nothing.
    current = dict(stored, entitlement=dict(stored['entitlement'], state='verified_current', enforce=True))
    resolved = {'printers': [TEN]}
    assert str(NINE) not in policy._exemptions(policy_db, current, resolved, expired)
    assert resolved['expiry_applied'] == past  # recorded once applied
    # A later selection change on the already-expired license cuts off at the change,
    # so the print (allowed while 9 was selected) is kept, including after the state
    # label went through verification_unavailable and back.
    for label in ('verified_expired', 'verification_unavailable'):
        after = dict(stored, entitlement=dict(expired, state=label), expiry_applied=past)
        result = policy._exemptions(policy_db, after, {'printers': [TEN]}, expired)
        assert result[str(NINE)]['print_job_id'] == row['id'], label
    assert policy._valid_policy(dict(stored, expiry_applied=past))


def test_pending_candidate_with_a_different_nonce_is_not_promoted(policy_db):
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    record = {'kind': 'pending', 'candidates': [{'id': row['id'], 'nonce': 'not-this-attempt'}],
              'captured_at': 'x', 'cutoff': datetime.now(timezone.utc).isoformat(),
              'until': (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()}
    kept = policy._refresh(policy_db, NINE, record, policy._db_now(policy_db))
    assert kept['kind'] == 'pending' and kept['candidates'] == []


def test_ambiguous_pending_resolves_when_one_candidate_remains(policy_db):
    first = _attempt(policy_db, NINE, started_ago=900)
    second = _attempt(policy_db, NINE, started_ago=600)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    assert len(_record(policy_db, NINE)['candidates']) == 2
    policy_db.execute(text("UPDATE print_jobs SET status='completed', ended_at=:t WHERE id=:id"),
                      {'t': _utcnow(), 'id': first['id']})
    policy_db.commit()
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == second['id']


def test_bookkeeping_cancel_is_not_terminal_proof(policy_db):
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    policy_db.execute(text("UPDATE print_jobs SET status='cancelled', ended_at=NULL, error_code='CLOSED_UNKNOWN' "
                           "WHERE id=:id"), {'id': row['id']})
    policy_db.commit()
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'pending' and list(record['candidates']) == []
    snap = policy.snapshot(policy_db)
    assert policy.printer_allowed(snap, NINE, 'M')  # bounded identity reconciliation only
    assert not policy.attempt_allowed(policy_db, snap, NINE, 'C')


def test_deactivating_a_printing_member_captures_its_print(policy_db):
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    policy_db.execute(text('UPDATE printers SET is_active=FALSE WHERE id=:p'), {'p': NINE})
    policy_db.commit()
    p = _reconcile(policy_db)
    assert p['exempt_attempts'][str(NINE)]['print_job_id'] == row['id']
    policy_db.execute(text('UPDATE printers SET is_active=TRUE WHERE id=:p'), {'p': NINE})
    policy_db.commit()
    p = _reconcile(policy_db)
    # Reactivated while 10 holds the seat: the same attempt keeps its record.
    assert NINE in p['printers'] or p['exempt_attempts'][str(NINE)]['print_job_id'] == row['id']


def test_admitted_resolves_through_retained_receipt_after_claim_is_gone(policy_db):
    token = 'v' * 32
    _claim_and_admit(policy_db, NINE, token)
    _lock_nine(policy_db)
    assert _record(policy_db, NINE)['kind'] == 'admitted'
    row = _attempt(policy_db, NINE, started_ago=5)
    _telemetry(policy_db, NINE)
    _bind_receipt(policy_db, NINE, token, row['id'])
    policy_db.execute(text('DELETE FROM printer_dispatch_claims'))  # claim retired; receipt kept
    policy_db.commit()
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']


def test_retained_receipt_is_found_among_many_receipts(policy_db):
    token = 'w' * 32
    _claim_and_admit(policy_db, NINE, token, job_id=7)
    _lock_nine(policy_db)
    row = _attempt(policy_db, NINE, started_ago=5)
    _telemetry(policy_db, NINE)
    _bind_receipt(policy_db, NINE, token, row['id'], job_id=7)
    for n in range(60):  # newer receipts for other jobs on the same printer
        _bind_receipt(policy_db, NINE, f'{n:032d}', None, job_id=100 + n)
    policy_db.execute(text('DELETE FROM printer_dispatch_claims'))
    policy_db.commit()
    _reconcile(policy_db)
    record = _record(policy_db, NINE)
    assert record['kind'] == 'observed' and record['print_job_id'] == row['id']


# --- B2b-2: completion-only ticket ------------------------------------------------

def _finish(db, row, ago=10):
    db.execute(text("UPDATE print_jobs SET status='completed', ended_at=:t WHERE id=:id"),
               {'t': _utcnow() - timedelta(seconds=ago), 'id': row['id']})
    db.commit()


def _plug(db, printer_id, auto_off):
    db.execute(text("UPDATE printers SET plug_type='tasmota', plug_host='10.0.0.9', plug_auto_off=:a WHERE id=:p"),
               {'a': auto_off, 'p': printer_id})
    db.commit()


def test_completion_ticket_allows_only_energy_and_configured_cooldown(policy_db):
    engine = policy_db.get_bind()
    _plug(policy_db, NINE, True)
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    assert policy.completion_allowed(engine, NINE, row['id'], 'energy')  # running exempt print
    assert not policy.completion_allowed(engine, NINE, row['id'], 'cooldown_off')  # not ended yet
    _finish(policy_db, row)
    assert policy.completion_allowed(engine, NINE, row['id'], 'cooldown_off')  # before reconcile catches up
    _reconcile(policy_db)
    assert policy.completion_allowed(engine, NINE, row['id'], 'cooldown_off')
    assert policy.completion_allowed(engine, NINE, row['id'], 'energy')
    assert not policy.completion_allowed(engine, NINE, row['id'] + 1, 'energy')  # another attempt
    assert not policy.completion_allowed(engine, NINE, None, 'cooldown_off')
    with pytest.raises(ValueError):
        policy.completion_allowed(engine, NINE, row['id'], 'power_on')


def test_cooldown_needs_auto_off_configured_at_cutoff(policy_db):
    engine = policy_db.get_bind()
    _plug(policy_db, NINE, False)
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    _plug(policy_db, NINE, True)  # enabling auto-off after the cutoff adds nothing
    _finish(policy_db, row)
    _reconcile(policy_db)
    assert not policy.completion_allowed(engine, NINE, row['id'], 'cooldown_off')
    assert policy.completion_allowed(engine, NINE, row['id'], 'energy')


def test_completion_ends_after_24_hours_or_when_another_print_starts(policy_db):
    engine = policy_db.get_bind()
    _plug(policy_db, NINE, True)
    row = _attempt(policy_db, NINE, started_ago=30 * 3600)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    _finish(policy_db, row, ago=25 * 3600)
    assert not policy.completion_allowed(engine, NINE, row['id'], 'cooldown_off')
    assert _reconcile(policy_db)['exempt_attempts'] == {}


def test_cooldown_refused_once_a_different_print_runs(policy_db):
    engine = policy_db.get_bind()
    _plug(policy_db, NINE, True)
    row = _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    _lock_nine(policy_db)
    _finish(policy_db, row)
    _reconcile(policy_db)
    _attempt(policy_db, NINE, started_ago=5)
    assert not policy.completion_allowed(engine, NINE, row['id'], 'cooldown_off')


def test_members_always_pass_and_rollout_off_is_a_no_op(policy_db, monkeypatch):
    engine = policy_db.get_bind()
    assert policy.completion_allowed(engine, NINE, None, 'cooldown_off')
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    assert policy.completion_allowed(engine, TEN, None, 'cooldown_off')


def test_power_fence_refuses_before_any_lock_for_a_locked_printer(policy_db, monkeypatch):
    from types import SimpleNamespace as NS
    from modules.printers import power_fence
    monkeypatch.setattr(power_fence, '_snapshot', lambda *a, **k: (NS(target_key='t'), {}))
    monkeypatch.setattr(power_fence, 'acquire_target_lock', lambda *a, **k: pytest.fail('target lock taken'))
    module = NS(engine=policy_db.get_bind())
    assert power_fence.run(module, TEN, 'manual_on') is None
    assert power_fence.run(module, TEN, 'observed_on', physical_id=1) is None  # never auto-on
    assert power_fence.run(module, TEN, 'cooldown_off', physical_id=1) is None  # no ticket


def test_unfenced_auto_on_skips_a_locked_printer(policy_db, monkeypatch):
    from modules.printers import smart_plug
    monkeypatch.setattr(smart_plug, 'engine', policy_db.get_bind())
    monkeypatch.setattr(smart_plug, 'get_plug_config', lambda pid, **k: {'type': 'homeassistant', 'auto_on': True})
    monkeypatch.setattr(smart_plug, 'power_on', lambda pid, **k: pytest.fail('auto-on sent'))
    smart_plug._unfenced_on_print_start(TEN)


def test_energy_reads_refused_for_a_locked_printer(policy_db, monkeypatch):
    from modules.printers import smart_plug
    monkeypatch.setattr(smart_plug, 'engine', policy_db.get_bind())
    monkeypatch.setattr(smart_plug, '_energy_counter', lambda pid: pytest.fail('plug read'))
    smart_plug.record_energy_start(TEN, 123)
    with smart_plug._energy_lock:
        smart_plug._energy_starts[456] = (TEN, 1.0)
    assert smart_plug.record_energy_for_print(TEN, 456) is None
