"""Quiet-hours deferred external deliveries (table from migration 032).

When quiet hours suppress an alert's email/push delivery for a recipient who has
no in-app alert row, the delivery is recorded here instead of being lost; the
quiet-hours digest delivers it after the window and marks it sent (or failed).
Recipients with an in-app row are already covered by the alerts table.

Every function accepts a SQLAlchemy Connection or Session and runs inside the
caller's transaction. When the table is absent (a database not yet upgraded by
migration 032) recording is a no-op and reads return nothing, i.e. exactly the
pre-032 behaviour.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, text

from core.quiet_hours_deferral_schema import CHANNELS, TABLE

log = logging.getLogger("odin.notifications")

SENT_RETENTION_DAYS = 7
UNSENT_RETENTION_DAYS = 30


def _bind(executor):
    return executor.get_bind() if hasattr(executor, "get_bind") else executor


def available(executor) -> bool:
    try:
        bind = executor.connection() if hasattr(executor, "connection") and hasattr(executor, "get_bind") else executor
        return inspect(bind).has_table(TABLE)
    except Exception:
        return False


def webhook_destination_exists(executor, org_id, alert_type) -> bool:
    """Whether a suppressed alert would have reached any webhook in real time:
    its org's settings webhook (org-owned) or a selecting global webhook (unscoped)."""
    import json
    if org_id is not None:
        raw = executor.execute(text("SELECT settings_json FROM groups WHERE id=:id"), {"id": org_id}).scalar()
        try:
            settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            return False
        return bool(isinstance(settings, dict) and settings.get("webhook_url"))
    from modules.notifications.event_selection import selects_event
    hooks = executor.execute(text("SELECT alert_types FROM webhooks WHERE is_enabled IS TRUE")).scalars().all()
    return any(selects_event(selection, str(alert_type).lower()) for selection in hooks)


def record_suppressed(executor, *, org_id, scope, recipients, alert_type, severity, title, message=None,
                      printer_id=None, job_id=None, spool_id=None, webhook=True) -> int:
    """Record suppressed deliveries of one alert.

    ``scope`` is the quiet-hours scope that caused the suppression ('system' or
    'org:<id>', see quiet_hours.governing_scope); the matching digest delivers the
    rows. ``recipients`` maps user_id -> {'email','push'} for recipients without an
    in-app row; ``webhook`` adds the one per-alert webhook row.
    """
    if not available(executor):
        return 0
    base = {"org_id": org_id, "scope": scope, "atype": str(alert_type).lower(), "sev": str(severity).lower(),
            "title": title, "msg": message, "pid": printer_id, "jid": job_id, "sid": spool_id}
    rows = []
    for user_id, channels in sorted((recipients or {}).items()):
        for channel in sorted(set(channels) & {"email", "push"}):
            rows.append({**base, "id": uuid.uuid4().hex, "user_id": int(user_id), "channel": channel})
    if webhook:
        rows.append({**base, "id": uuid.uuid4().hex, "user_id": None, "channel": "webhook"})
    for row in rows:
        executor.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table is a module constant; channel/state literals come from fixed tuples; values are bound
            f"INSERT INTO {TABLE}(id,user_id,org_id,scope_key,channel,alert_type,severity,title,message,"
            "printer_id,job_id,spool_id,state) VALUES(:id,:user_id,:org_id,:scope,:channel,:atype,:sev,:title,"
            ":msg,:pid,:jid,:sid,'pending')"), row)
    if rows:
        log.info("Quiet hours deferred %s deliveries (%s) for the digest", len(rows), scope)
    return len(rows)


def pending_for_window(executor, scope, window_start, window_end, channels=("email", "push")) -> list:
    """Pending rows recorded under ``scope`` inside the window, as alert-shaped dicts."""
    if not available(executor):
        return []
    dialect = _bind(executor).dialect.name
    predicate = ("datetime(created_at)>=datetime(:start) AND datetime(created_at)<datetime(:end)"
                 if dialect == "sqlite" else "created_at>=:start AND created_at<:end")
    wanted = [c for c in channels if c in CHANNELS]
    if not wanted:
        return []
    channel_list = ",".join(f"'{c}'" for c in wanted)  # literals from the fixed CHANNELS tuple
    rows = executor.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table is the TABLE constant; channels checked against the CHANNELS allowlist; values are bound
        f"SELECT id,user_id,org_id,channel,alert_type,severity,title,message,printer_id,job_id,spool_id,created_at "
        f"FROM {TABLE} WHERE scope_key=:scope AND state='pending' AND channel IN ({channel_list}) AND {predicate} "
        "ORDER BY created_at, id"),
        {"scope": scope, "start": _sqlite_stamp(window_start, dialect),
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
        result = executor.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table is a module constant; channel/state literals come from fixed tuples; values are bound
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
        result = executor.execute(text(f"DELETE FROM {TABLE} WHERE state IN ({placeholders}) AND {clock}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table is a module constant; channel/state literals come from fixed tuples; values are bound
                                  {"cutoff": cutoff})
        removed += result.rowcount or 0
    return removed
