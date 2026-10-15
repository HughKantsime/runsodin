"""Quiet-hours deferred external deliveries (table from migration 032).

When quiet hours suppress an alert's email/push delivery for a recipient who has
no in-app alert row, the delivery is recorded here instead of being lost; the
quiet-hours digest delivers it after the window and marks it sent (or failed).
Recipients with an in-app row are already covered by the alerts table.

Every function accepts a SQLAlchemy Connection or Session and runs inside the
caller's transaction. When the table is absent (032 not yet registered) recording
is a no-op and reads return nothing, i.e. exactly the pre-032 behaviour.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, text

from core.quiet_hours_deferral_schema import CHANNELS, TABLE

log = logging.getLogger("odin.notifications")

SENT_RETENTION_DAYS = 7
UNSENT_RETENTION_DAYS = 30


def scope_key(org_id):
    return "system" if org_id is None else f"org:{int(org_id)}"


def _bind(executor):
    return executor.get_bind() if hasattr(executor, "get_bind") else executor


def available(executor) -> bool:
    try:
        bind = executor.connection() if hasattr(executor, "connection") and hasattr(executor, "get_bind") else executor
        return inspect(bind).has_table(TABLE)
    except Exception:
        return False


def record_suppressed(executor, *, org_id, recipients, alert_type, severity, title, message=None,
                      printer_id=None, job_id=None, spool_id=None) -> int:
    """Record one row per (user, channel) in ``recipients`` ({user_id: {'email','push'}})."""
    if not recipients or not available(executor):
        return 0
    rows = []
    for user_id, channels in sorted(recipients.items()):
        for channel in sorted(set(channels) & set(CHANNELS)):
            rows.append({"id": uuid.uuid4().hex, "user_id": int(user_id), "org_id": org_id,
                         "scope": scope_key(org_id), "channel": channel, "atype": str(alert_type).lower(),
                         "sev": str(severity).lower(), "title": title, "msg": message,
                         "pid": printer_id, "jid": job_id, "sid": spool_id})
    for row in rows:
        executor.execute(text(
            f"INSERT INTO {TABLE}(id,user_id,org_id,scope_key,channel,alert_type,severity,title,message,"
            "printer_id,job_id,spool_id,state) VALUES(:id,:user_id,:org_id,:scope,:channel,:atype,:sev,:title,"
            ":msg,:pid,:jid,:sid,'pending')"), row)
    if rows:
        log.info("Quiet hours deferred %s external deliveries for later digest", len(rows))
    return len(rows)


def pending_for_window(executor, org_id, window_start, window_end) -> list:
    """Pending rows of one scope created inside the window, as alert-shaped dicts."""
    if not available(executor):
        return []
    dialect = _bind(executor).dialect.name
    predicate = ("datetime(created_at)>=datetime(:start) AND datetime(created_at)<datetime(:end)"
                 if dialect == "sqlite" else "created_at>=:start AND created_at<:end")
    rows = executor.execute(text(
        f"SELECT id,user_id,channel,alert_type,severity,title,message,printer_id,job_id,spool_id,created_at "
        f"FROM {TABLE} WHERE scope_key=:scope AND state='pending' AND {predicate} ORDER BY created_at, id"),
        {"scope": scope_key(org_id), "start": _sqlite_stamp(window_start, dialect),
         "end": _sqlite_stamp(window_end, dialect)}).mappings().all()
    return [dict(row) for row in rows]


def _sqlite_stamp(value, dialect):
    if dialect != "sqlite":
        return value
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def mark(executor, ids, state, reason=None) -> int:
    """Record the digest outcome for rows still pending (idempotent)."""
    if not ids or state not in ("sent", "failed"):
        return 0
    count = 0
    for row_id in ids:
        result = executor.execute(text(
            f"UPDATE {TABLE} SET state=:state, outcome_reason=:reason, delivered_at=CURRENT_TIMESTAMP "
            "WHERE id=:id AND state='pending'"), {"state": state, "reason": reason, "id": row_id})
        count += result.rowcount or 0
    return count


def purge(executor, now=None) -> int:
    """Retention: sent rows after SENT_RETENTION_DAYS, failed/pending after UNSENT_RETENTION_DAYS."""
    if not available(executor):
        return 0
    now = now or datetime.now(timezone.utc)
    dialect = _bind(executor).dialect.name
    clock = "datetime(created_at)<datetime(:cutoff)" if dialect == "sqlite" else "created_at<:cutoff"
    removed = 0
    for states, days in ((("sent",), SENT_RETENTION_DAYS), (("failed", "pending"), UNSENT_RETENTION_DAYS)):
        cutoff = _sqlite_stamp(now - timedelta(days=days), dialect)
        placeholders = ",".join(f"'{state}'" for state in states)  # fixed literals only
        result = executor.execute(text(f"DELETE FROM {TABLE} WHERE state IN ({placeholders}) AND {clock}"),
                                  {"cutoff": cutoff})
        removed += result.rowcount or 0
    return removed
