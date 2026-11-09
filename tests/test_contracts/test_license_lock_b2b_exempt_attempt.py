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
    assert record['kind'] == 'pending' and record['print_job_id'] == row['id']
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
    assert record['kind'] == 'pending' and record['print_job_id'] is None


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
    assert str(NINE) not in p['exempt_attempts'] and p['revision'] > before
    _attempt(policy_db, NINE, started_ago=10)  # a later print on the locked printer
    _reconcile(policy_db)
    assert _record(policy_db, NINE) is None


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
    assert _record(policy_db, NINE) is None


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
