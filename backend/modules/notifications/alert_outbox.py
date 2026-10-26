"""Transactional outbox for job create/approval alerts (table: migration 034).

``enqueue`` writes one small row inside the caller's transaction, so the alert is
recorded atomically with the job change at the cost of one INSERT. After the commit,
``dispatch`` claims the row and, in one transaction of its own, deletes it and stages
the alert fan-out (alert rows, preference seeding, deferred-delivery records): the
alert and the claim commit together, so a row yields exactly one alert however often
it is dispatched. External delivery (push, email, webhook) follows that commit, best
effort and never retried, as before.

A row left by a crash or a failed dispatch is replayed by ``sweep`` at startup and
periodically. A failed dispatch increments ``attempts``; after MAX_ATTEMPTS the row is
no longer retried, kept DEAD_LETTER_RETENTION_DAYS for inspection, then pruned.
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.base import AlertSeverity, AlertType

logger = logging.getLogger("odin.alert_outbox")

_migration = importlib.import_module("core.schema.migrations.034_alert_outbox")
TABLE = _migration.TABLE
KINDS = _migration.KINDS
MAX_ATTEMPTS = 5
REPLAY_AFTER_SECONDS = 5
# Rows that exhausted their attempts are kept this long for inspection, then pruned.
DEAD_LETTER_RETENTION_DAYS = 30


def enqueue(db: Session, job_id: int, kind: str, actor_user_id) -> int:
    """Record the alert a job change owes, inside the caller's transaction."""
    if kind not in KINDS:
        raise ValueError("Unknown outbox alert kind")
    from core.db_compat import execute_insert_returning_id
    return execute_insert_returning_id(
        db, "INSERT INTO alert_outbox (job_id, alert_kind, actor_user_id) VALUES (:job, :kind, :actor)",
        {"job": job_id, "kind": kind, "actor": actor_user_id})


def _alert_for(db, kind, job, actor):
    """(type, title, message, recipients) for an outbox row, from current rows."""
    from modules.notifications.alert_dispatcher import get_job_approval_reviewer_ids
    name = job["item_name"] or "Untitled"
    actor_name = (actor or {}).get("username")
    if kind == "job_submitted":
        if actor is None:
            return None
        return (AlertType.JOB_SUBMITTED, f"Job awaiting approval: {name}",
                f"{actor_name or 'A user'} submitted a print job", get_job_approval_reviewer_ids(db, dict(actor)))
    if kind == "queue_added":
        if actor is None:
            return None
        return (AlertType.QUEUE_ADDED, f"Job added to queue: {name}", "", [actor["id"]])
    if kind == "job_approved":
        if job["submitted_by"] is None:
            return None
        return (AlertType.JOB_APPROVED, f"Job approved: {name}", f"Approved by {actor_name or 'an approver'}",
                [job["submitted_by"]])
    raise ValueError("Unknown outbox alert kind")


def dispatch(engine, outbox_id: int) -> bool:
    """Claim one outbox row and stage its alert exactly once. True when this call staged it."""
    from core.dispatch_connection import dispatch_scope, dispatch_transaction
    from modules.notifications.alert_dispatcher import _deliver_staged_alert, stage_alert
    plan = None
    try:
        with dispatch_scope(engine):
            summary = None
            try:
                with dispatch_transaction(engine) as read_conn, Session(bind=read_conn) as read_db:
                    kind = read_db.execute(text('SELECT alert_kind FROM alert_outbox WHERE id=:id'), {'id':outbox_id}).scalar_one_or_none()
                    uid = None
                    if kind and kind.startswith('terminal:'):
                        uid = read_db.execute(text('SELECT economic_uid FROM print_jobs WHERE id=:id'), {'id':int(kind.split(':',1)[1])}).scalar_one_or_none()
                    if uid:
                        from modules.jobs.completion_summary import build_summary
                        summary = build_summary(read_db, uid)
            except Exception:
                logger.warning("Completion summary unavailable; preserving original terminal alert")
            with dispatch_transaction(engine) as connection, Session(bind=connection) as db:
                row = db.execute(text("SELECT job_id, alert_kind, actor_user_id FROM alert_outbox WHERE id=:id"),
                                 {"id": outbox_id}).mappings().first()
                if row is None:
                    return False
                # The claim: deleting the row commits with the alert rows, so a second
                # dispatcher of the same row finds nothing to claim.
                if db.execute(text("DELETE FROM alert_outbox WHERE id=:id"), {"id": outbox_id}).rowcount != 1:
                    return False
                if row['alert_kind'].startswith('terminal:'):
                    plan = _stage_terminal(db, row, outbox_id, summary=summary)
                else:
                    job = db.execute(text("SELECT id, item_name, submitted_by FROM jobs WHERE id=:id"),
                                     {"id": row["job_id"]}).mappings().first()
                    actor = None
                    if row["actor_user_id"] is not None:
                        actor = db.execute(text("SELECT id, username, role, group_id FROM users WHERE id=:id"),
                                           {"id": row["actor_user_id"]}).mappings().first()
                    alert = _alert_for(db, row["alert_kind"], job, actor) if job is not None else None
                    if alert is not None:
                        alert_type, title, message, recipients = alert
                        plan = stage_alert(db, alert_type, AlertSeverity.INFO, title, message,
                                           job_id=job["id"], target_user_ids=recipients)
                db.flush()
            if plan is not None:
                with dispatch_transaction(engine) as delivery_conn, Session(bind=delivery_conn) as delivery_db:
                    _deliver_staged_alert(delivery_db, plan)
                    deliver_mobile(delivery_db, plan)
        return True
    except Exception:
        logger.warning("Alert outbox dispatch failed for row %s; it will be replayed", outbox_id)
        try:
            with engine.begin() as connection:
                connection.execute(text("UPDATE alert_outbox SET attempts = attempts + 1 WHERE id=:id"), {"id": outbox_id})
        except Exception:
            logger.warning("Alert outbox attempt count not recorded for row %s", outbox_id)
        return False


def sweep(engine, *, older_than_seconds: float = REPLAY_AFTER_SECONDS, limit: int = 100) -> int:
    """Replay outbox rows a crash or a failed dispatch left behind. Returns rows staged."""
    from sqlalchemy import inspect
    if not inspect(engine).has_table(TABLE):
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=older_than_seconds)
    expired = {"max": MAX_ATTEMPTS, "expired": now - timedelta(days=DEAD_LETTER_RETENTION_DAYS)}
    with engine.connect() as connection:  # read first: an idle sweep takes no write lock
        prune = connection.execute(text("SELECT 1 FROM alert_outbox WHERE attempts >= :max AND created_at < :expired LIMIT 1"),
                                   expired).first() is not None
    if prune:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alert_outbox WHERE attempts >= :max AND created_at < :expired"), expired)
    with engine.connect() as connection:
        ids = [row[0] for row in connection.execute(text(
            "SELECT id FROM alert_outbox WHERE attempts < :max AND created_at <= :cutoff ORDER BY id LIMIT :limit"),
            {"max": MAX_ATTEMPTS, "cutoff": cutoff, "limit": limit})]
    return sum(1 for outbox_id in ids if dispatch(engine, outbox_id))


SWEEP_INTERVAL_SECONDS = 30


async def run_sweeper(interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    """Lifespan task: replay left-behind rows at startup, then every ``interval`` seconds.

    At startup every row is replayed whatever its age (its process may have died a
    moment ago); later sweeps skip rows younger than REPLAY_AFTER_SECONDS, which their
    own request is still dispatching. The claim keeps either case exactly-once."""
    import asyncio
    from core.db import current_engine
    older_than = 0.0
    while True:
        try:
            replayed = await asyncio.to_thread(sweep, current_engine(), older_than_seconds=older_than)
            if replayed:
                logger.info("Alert outbox replayed %d row(s)", replayed)
        except Exception:
            logger.warning("Alert outbox sweep failed; it runs again at the next interval")
        older_than = REPLAY_AFTER_SECONDS
        await asyncio.sleep(interval)


_deliveries = []


def register_delivery(callback):
    if callback not in _deliveries:
        _deliveries.append(callback)


def deliver_mobile(db, plan):
    import sys
    from modules.push.fanout import register_subscribers
    register_subscribers(sys.modules[__name__])
    for callback in tuple(_deliveries):
        try:callback(db, plan)
        except Exception:logger.warning("Mobile alert delivery failed after confirmed commit")


def _stage_terminal(db, row, outbox_id, *, summary=None):
    """Resolve the immutable physical claim; never send for a recycled print ID."""
    from modules.notifications.alert_dispatcher import stage_alert
    physical_id = int(row['alert_kind'].split(':', 1)[1])
    attempt = db.execute(text("SELECT s.outcome,s.delivery_state,s.printer_id,s.job_id,p.job_name,r.name FROM job_summary_outbox s JOIN print_jobs p ON p.economic_uid=s.print_attempt_uid AND p.economic_birth_nonce=s.attempt_birth LEFT JOIN printers r ON r.id=s.printer_id WHERE p.id=:id AND s.job_id=:job"), {'id':physical_id,'job':row['job_id']}).first()
    if attempt is None:
        return None
    outcome, state, printer_id, job_id, name, printer_name = attempt
    state = json.loads(state)
    if state.get('alert_outbox_id') != outbox_id:
        return None
    payload = state.get('alert_payload')
    if payload is None:
        # Crash before post-commit dispatch: the transaction already saved enough
        # facts to deliver without trusting an in-memory monitor callback.
        complete = outcome == 'completed'
        payload = dict(alert_type=AlertType.PRINT_COMPLETE if complete else AlertType.PRINT_FAILED,
                       severity=AlertSeverity.INFO if complete else AlertSeverity.CRITICAL,
                       title=f"Print {'Complete' if complete else 'Failed'}: {name or 'Unknown'}",
                       message=f"{'Finished' if complete else 'Failed'} on {printer_name or 'printer'}",
                       printer_id=printer_id, job_id=state.get('alert_job_id',job_id))
    else:
        payload['alert_type'] = AlertType.PRINT_FAILED if payload['alert_type'] == 'print_cancelled' else AlertType(payload['alert_type'])
        payload['severity'] = AlertSeverity(payload['severity'])
    if summary is not None:
        payload['metadata'] = {**(payload.get('metadata') or {}), 'summary': summary}
        payload['message'] = summary['text']
    return stage_alert(db, **payload)
