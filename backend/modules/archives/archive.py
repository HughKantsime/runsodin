"""
O.D.I.N. — Print Archive

Auto-captures a print archive record when a job completes.
Called from printer_events.job_completed().
"""

import logging

from core.db_compat import sql
# Resolved at call time: never a stale get_db bound at import.
from core.db_utils import live_get_db as get_db

log = logging.getLogger("odin.archive")


def create_print_archive(print_job_id: int, printer_id: int, success: bool,
                         result_status: str = None):
    """Create exactly one archive and account locally in one transaction.

    Provider delivery is queued durably; no request occurs inside this commit.
    Missing historical attribution produces an archive requiring reconciliation.
    """
    import json
    import math
    from core.db_compat import as_utc_datetime
    from .attempt_accounting import account_archive
    try:
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("UPDATE print_jobs SET id=id WHERE id=? AND printer_id=? AND status IN ('completed','failed','cancelled')",
                            (print_job_id, printer_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
                cur.execute('SELECT id FROM print_archives WHERE print_job_id=? ORDER BY id', (print_job_id,))
                existing = cur.fetchone()
                if existing:
                    conn.commit()
                    return existing[0]
                cur.execute('SELECT job_name,started_at,ended_at,scheduled_job_id,status,'
                            'spool_snapshot,filament_used_g,print_duration_seconds '
                            'FROM print_jobs WHERE id=?', (print_job_id,))
                pj = cur.fetchone()
                name, started, ended, scheduled, stored_status, snapshot_json, measured, duration = pj
                if result_status is not None and result_status != stored_status:
                    conn.rollback()
                    return None
                status = stored_status
                if duration is None and started and ended:
                    try:
                        duration = (as_utc_datetime(ended)-as_utc_datetime(started)).total_seconds()
                    except (ValueError, TypeError):
                        duration = None
                if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
                    duration = None
                cur.execute('SELECT org_id FROM printers WHERE id=?', (printer_id,))
                org = cur.fetchone()[0]
                user_id = cost = explicit_file = None
                job_present = scheduled is None
                if scheduled is not None:
                    cur.execute('SELECT submitted_by,estimated_cost,print_file_id,charged_to_org_id FROM jobs WHERE id=?', (scheduled,))
                    job = cur.fetchone()
                    if job:
                        job_present = True
                        user_id, cost, explicit_file = job[:3]
                        org = job[3] if job[3] is not None else org
                pf = None
                if explicit_file is not None:
                    cur.execute('SELECT id,thumbnail_b64,stored_path,filament_weight_grams,plate_count,org_id FROM print_files WHERE id=?', (explicit_file,))
                    pf = cur.fetchone()
                elif scheduled is not None:
                    cur.execute('SELECT id,thumbnail_b64,stored_path,filament_weight_grams,plate_count,org_id FROM print_files WHERE job_id=? ORDER BY id', (scheduled,))
                    files = cur.fetchall()
                    if len(files) == 1:
                        pf = files[0]
                if pf is not None and pf[5] is not None and pf[5] != org:
                    pf = None
                file_id, thumbnail, path, estimate, plates = pf[:5] if pf else (None, None, None, None, 1)
                grams = measured if measured is not None else estimate
                if grams is not None and (type(grams) not in (int, float) or not math.isfinite(grams) or grams < 0):
                    grams = None
                # The start plan remains immutable on disk. A changed current
                # graph cannot authorize deductions from its historical tenant.
                captured = None
                accounting_snapshot = snapshot_json
                try:
                    captured = json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
                    plan = captured.get('material_plan') if isinstance(captured, dict) else None
                    if isinstance(plan, dict) and (not job_present or plan.get('org_id') != org
                            or (plan.get('print_file_id') is not None and plan.get('print_file_id') != file_id)):
                        captured = dict(captured)
                        captured['material_plan'] = dict(plan, state='unresolved', reason='archive_graph_changed', allocations=[])
                        accounting_snapshot = captured
                except (ValueError, TypeError):
                    pass  # account_archive records an explicit invalid-snapshot reason.
                from .ownership import capture_content_owner
                current_owner = capture_content_owner(conn, scheduled)
                captured_owner = captured.get('content_owner') if isinstance(captured, dict) else None
                owner = dict(current_owner)
                if isinstance(captured_owner, dict):
                    owner = dict(captured_owner)
                    keys = ('state', 'org_id', 'scheduled_job_id', 'print_file_id', 'education_submission_id')
                    if any(owner.get(key) != current_owner.get(key) for key in keys):
                        owner.update(state='unresolved', source='source_graph_changed')
                    elif owner.get('state') == 'known':
                        owner['source'] = 'first_start'
                if owner.get('state') != 'known':
                    # Never attach metadata from a changed/foreign source to
                    # a historical ownership anchor.
                    file_id = thumbnail = path = cost = user_id = None
                owner_org = owner.get('org_id')
                owner_state = owner.get('state', 'unresolved')
                owner_source = owner.get('source', 'missing_job_file')
                education_id = owner.get('education_submission_id')
                cur.execute(f"""INSERT INTO print_archives
                    (job_id,print_job_id,printer_id,user_id,print_name,status,started_at,completed_at,
                     actual_duration_seconds,filament_used_grams,cost_estimate,thumbnail_b64,file_path,print_file_id,plate_count,
                     org_id,ownership_state,ownership_source,education_submission_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?){sql.returning_id()}""",
                    (scheduled,print_job_id,printer_id,user_id,name or 'Unknown',status,started,ended,
                     duration,grams,cost,thumbnail,path,file_id,plates or 1,
                     owner_org,owner_state,owner_source,education_id))
                archive_id = cur.fetchone()[0] if sql.is_postgres else cur.lastrowid
                if owner_state == 'known':
                    accounting_state, reason, _ = account_archive(conn, archive_id=archive_id,
                        print_job_id=print_job_id, printer_id=printer_id, snapshot_json=accounting_snapshot,
                        scheduled_job_id=scheduled, measured_grams=measured, success=status=='completed')
                else:
                    accounting_state, reason = 'unresolved', 'archive_ownership_unresolved'
                cur.execute('UPDATE print_archives SET accounting_state=?,accounting_reason=? WHERE id=?',
                            (accounting_state, reason, archive_id))
                conn.commit()
                return archive_id
            except BaseException:
                conn.rollback()
                raise
    except Exception as exc:
        log.error('Archive persistence failed for attempt %s (%s); reconciliation required',
                  print_job_id, type(exc).__name__)
        return None


def _sync_spoolman_consumption(spoolman_spool_id: int, grams_used: float):
    """Deliver one consumption record to Spoolman; return (state, reason).

    Delegates to the inventory module's single guarded Spoolman client
    (DNS pinning, no proxies/redirects, ITAR refusal, PUT /use).
    """
    from modules.inventory.services import deliver_spoolman_usage
    return deliver_spoolman_usage(spoolman_spool_id, grams_used)


_OUTBOX_STATES = ("sent", "failed", "pending", "outcome_unknown")
# Per-process claim token, recorded on rows this process holds in flight.
import uuid as _uuid  # noqa: E402
_PROCESS_CLAIM = "claim:" + _uuid.uuid4().hex
# A delivery attempt takes at most a few seconds (5 s HTTP timeout); an in-flight
# claim older than this belongs to a process that stopped mid-delivery.
STALE_CLAIM_MINUTES = 15


def deliver_filament_outbox(limit: int = 100) -> dict:
    """Deliver queued printer-observed consumption (filament_delivery_outbox) to Spoolman.

    Runs after the archive transaction committed, never inside it. Each row is
    claimed atomically (pending -> in_flight) so concurrent runners never send
    a row twice; the result is recorded on the row and logged, so a failure is
    never silent. Rows that may have reached Spoolman become 'outcome_unknown'
    and are not resent.
    """
    counts = {state: 0 for state in _OUTBOX_STATES}
    counts["skipped"] = 0
    from core.config import settings
    if not (settings.spoolman_url or "").strip():
        # Integration not configured: leave rows pending, untouched, until it is.
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM filament_delivery_outbox WHERE state='pending'")
            counts["skipped"] = cur.fetchone()[0]
        return counts
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, spoolman_spool_id, grams_used, print_job_id FROM filament_delivery_outbox "
                    "WHERE state='pending' ORDER BY created_at, id LIMIT ?", (limit,))
        rows = cur.fetchall()
    for outbox_id, spoolman_id, grams, print_job_id in rows:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE filament_delivery_outbox SET state='in_flight', attempt_started_at=CURRENT_TIMESTAMP, "
                        "outcome_reason=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='pending'",
                        (_PROCESS_CLAIM, outbox_id))
            claimed = cur.rowcount == 1
            conn.commit()
        if not claimed:
            continue
        try:
            outcome = _sync_spoolman_consumption(spoolman_id, grams)
        except Exception as exc:
            outcome = ("outcome_unknown", f"delivery_error: {type(exc).__name__}")
        state, reason = outcome if (isinstance(outcome, tuple) and len(outcome) == 2
                                    and outcome[0] in _OUTBOX_STATES) else ("outcome_unknown", "no_delivery_result")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE filament_delivery_outbox SET state=?, outcome_reason=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND state='in_flight' AND outcome_reason=?", (state, reason, outbox_id, _PROCESS_CLAIM))
            conn.commit()
        counts[state] += 1
        if state == "sent":
            log.info("Spoolman usage delivered: print_job=%s spoolman_spool=%s grams=%s", print_job_id, spoolman_id, grams)
        else:
            log.warning("Spoolman usage %s: print_job=%s spoolman_spool=%s grams=%s reason=%s",
                        state, print_job_id, spoolman_id, grams, reason)
    return counts


def _deliver_outbox_in_background():
    import threading

    def run():
        try:
            deliver_filament_outbox()
        except Exception as exc:
            log.error("Spoolman outbox delivery run failed (%s); rows stay queued", type(exc).__name__)
    threading.Thread(target=run, daemon=True, name="spoolman-outbox").start()


def recover_stale_outbox_claims(max_age_minutes: int = STALE_CLAIM_MINUTES) -> int:
    """A row left 'in_flight' by a stopped process may or may not have reached
    Spoolman: record it as 'outcome_unknown' (visible, never resent).

    Only claims older than ``max_age_minutes`` (or without a start time) and not
    held by this process are touched, so a starting worker never steals the live
    claim of a sibling process that is delivering right now.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE filament_delivery_outbox SET state='outcome_unknown', "
                    "outcome_reason='interrupted_in_flight', updated_at=CURRENT_TIMESTAMP "
                    "WHERE state='in_flight' AND (outcome_reason IS NULL OR outcome_reason<>?) "
                    f"AND (attempt_started_at IS NULL OR attempt_started_at < {sql.now_offset(f'-{int(max_age_minutes)} minutes')})",
                    (_PROCESS_CLAIM,))
        count = cur.rowcount or 0
        conn.commit()
    if count:
        log.warning("Spoolman outbox: %s interrupted deliveries recorded as outcome_unknown", count)
    return count


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------

def _on_job_completed_event(event) -> None:
    """Auto-capture a print archive when a job finishes (success or failure)."""
    data = event.data
    print_job_id = data.get("print_job_id")
    printer_id = data.get("printer_id")
    success = data.get("success", True)
    if print_job_id is not None and printer_id is not None:
        try:
            create_print_archive(print_job_id, printer_id, success)
        except Exception as e:
            log.warning(f"Print archive capture failed via event bus: {e}")
        # The archive transaction has committed: deliver its queued consumption
        # (and any earlier retryable rows) to Spoolman off the monitor thread.
        _deliver_outbox_in_background()


def _on_job_cancelled_event(event) -> None:
    """Auto-capture a print archive when a job is cancelled."""
    data = event.data
    print_job_id = data.get("print_job_id")
    printer_id = data.get("printer_id")
    if print_job_id is not None and printer_id is not None:
        try:
            create_print_archive(print_job_id, printer_id, success=False,
                                 result_status="cancelled")
        except Exception as e:
            log.warning(f"Print archive capture (cancelled) failed via event bus: {e}")
        _deliver_outbox_in_background()


def register_subscribers(bus) -> None:
    """
    Register archive event handlers on the event bus.

    Called from modules/archives/__init__.py register_subscribers().
    """
    from core import events as ev

    bus.subscribe(ev.JOB_COMPLETED, _on_job_completed_event)
    bus.subscribe(ev.JOB_FAILED, _on_job_completed_event)
    bus.subscribe(ev.JOB_CANCELLED, _on_job_cancelled_event)
    log.debug("archive subscribed to event bus")
    try:
        recovered = recover_missing_archives()
        if recovered["attempted"]:
            log.info("Archive startup recovery: %s of %s attempts persisted", recovered["recovered"], recovered["attempted"])
        if recovered["pending_count"]:
            log.warning("Archive recovery backlog: %s attempts; next cursor %s", recovered["pending_count"], recovered["next_after_id"])
    except Exception as exc:
        log.error("Archive startup recovery unavailable (%s); terminal attempts retained", type(exc).__name__)
    try:
        recover_stale_outbox_claims()
        _deliver_outbox_in_background()
    except Exception as exc:
        log.error("Spoolman outbox startup delivery unavailable (%s); rows stay queued", type(exc).__name__)


def recover_missing_archives(limit: int = 100, *, after_attempt_id: int = 0):
    """Recover a bounded batch from durable terminal rows, with no provider I/O.

    Failed persistence remains in this query for a later recovery attempt.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Archive recovery limit must be 1 through 100')
    if type(after_attempt_id) is not int or after_attempt_id < 0:
        raise ValueError('Archive recovery cursor must be a nonnegative attempt ID')
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pj.id,pj.printer_id,pj.status FROM print_jobs pj "
                    "WHERE pj.id>? AND pj.status IN ('completed','failed','cancelled') "
                    "AND NOT EXISTS(SELECT 1 FROM print_archives a WHERE a.print_job_id=pj.id) "
                    "ORDER BY pj.id LIMIT ?", (after_attempt_id, limit))
        pending = cur.fetchall()
    recovered = 0
    for attempt_id, printer_id, status in pending:
        if create_print_archive(attempt_id, printer_id, status == 'completed', result_status=status) is not None:
            recovered += 1
    with get_db() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM print_jobs pj WHERE pj.status IN ('completed','failed','cancelled') "
            "AND NOT EXISTS(SELECT 1 FROM print_archives a WHERE a.print_job_id=pj.id)").fetchone()[0]
    return {'attempted': len(pending), 'recovered': recovered, 'pending_count': remaining,
            'next_after_id': pending[-1][0] if pending else None}
