"""The guardian's status file, how /health reads it, and how a stopped guardian gets reported.

The file sits outside the database on purpose, since the thing it reports on may be damaged.
Reading it is cheap and can't fail the health probe: anything unreadable counts as no status yet.
report_runner watches through this module, so it must not import modules.system.db_guardian: a
guardian that won't even import is exactly what the watcher is there to report.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.db_integrity import DAMAGED, RUNBOOK
from modules.system.backup_failure_alerts import stage_admin_alert
from modules.system.backup_service import BackupValidationError, paths_from_database_url

PENDING = "pending"
HEALTHY = "ok"
FAILED = "failed"
UNAVAILABLE = "unavailable"
STALE = "stale"
RETRYING = "retrying"
NOT_HEALTHY = frozenset({FAILED, UNAVAILABLE, STALE})
# One check that can't finish is usually a passing lock; three in a row means nothing is checking.
# Until then /health stays "ok": a passing lock must not fail a deploy's health window.
UNAVAILABLE_ALERT_AFTER = 3
# Grace beyond two missed intervals, so a slow check or a guardian restart doesn't flap /health.
STALE_GRACE_SECONDS = 300
_MAX_STATUS_BYTES = 64 * 1024
OVERDUE = "overdue"
BACKUP_MISSED_AFTER_SECONDS = 26 * 3600
ALERT_REPEAT = timedelta(days=1)

ALERT_TITLE = "Database integrity check failed"
_ALERT_MESSAGES = {
    DAMAGED: "ODIN's database failed its integrity check. ODIN won't repair it, and a restart "
             "will refuse to start until it is fixed. Follow the runbook before restarting: " + RUNBOOK,
    UNAVAILABLE: "ODIN's scheduled database integrity check has not been able to finish. The database "
                 "may be fine, but nothing is checking it. See the runbook: " + RUNBOOK,
}


def status_path(database: Path) -> Path:
    return database.parent / "run" / "db-guardian.json"


def offsite_path(database: Path) -> Path:
    """Written by modules.system.offsite_export when the off-server job confirms a copy."""
    return database.parent / "run" / "db-offsite.json"


def read_status(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            status = json.loads(handle.read(_MAX_STATUS_BYTES))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def integrity_view(status: dict, *, now: datetime | None = None) -> dict[str, str | None]:
    """The public summary: a state and a time, nothing about what is damaged."""
    now = now or datetime.now(timezone.utc)
    integrity = status.get("integrity") if isinstance(status.get("integrity"), dict) else {}
    checked_at = parse_time(integrity.get("checked_at"))
    interval = status.get("interval_seconds")
    limit = 2 * interval + STALE_GRACE_SECONDS if isinstance(interval, int) and interval > 0 else None
    state = integrity.get("state")
    if state == "damaged":
        # Stays failed however old: only a passing check clears it.
        return {"status": FAILED, "checked_at": integrity.get("checked_at")}
    if checked_at is None:
        started_at = parse_time(status.get("started_at"))
        overdue = started_at is not None and limit is not None and (now - started_at).total_seconds() > limit
        return {"status": STALE if overdue else PENDING, "checked_at": None}
    if limit is None or (now - checked_at).total_seconds() > limit:
        return {"status": STALE, "checked_at": integrity.get("checked_at")}
    if state == "unavailable":
        misses = integrity.get("consecutive_unavailable")
        retrying = isinstance(misses, int) and misses < UNAVAILABLE_ALERT_AFTER
        return {"status": RETRYING if retrying else UNAVAILABLE, "checked_at": integrity.get("checked_at")}
    return {"status": HEALTHY if state == "ok" else STALE, "checked_at": integrity.get("checked_at")}


def database_integrity(database_url: str) -> dict[str, str | None] | None:
    """The guardian's last result for /health. SQLite only; PostgreSQL has no guardian."""
    try:
        paths = paths_from_database_url(database_url)
    except BackupValidationError:
        return None
    return integrity_view(read_status(status_path(paths.database)))


def overall_status(integrity: dict[str, str | None] | None) -> str:
    return "degraded" if integrity and integrity["status"] in NOT_HEALTHY else "ok"


def backup_view(status: dict, *, now: datetime | None = None) -> dict[str, str | None]:
    """The public summary of the backups: a state and a time, no paths, names, sizes or reasons.

    Worked out from time as well as the file, so it stays true when nothing can be saved: with the
    disk too full to write, it keeps the last saved state, shows stale once checks stop being
    recorded, and overdue once the last good backup is too old.
    """
    now = now or datetime.now(timezone.utc)
    backup = status.get("backup") if isinstance(status.get("backup"), dict) else {}
    last_success = parse_time(backup.get("last_success_at"))
    shown = backup.get("last_success_at") if last_success else None
    reference = last_success or parse_time(status.get("started_at"))
    if reference is not None and (now - reference).total_seconds() >= BACKUP_MISSED_AFTER_SECONDS:
        return {"status": OVERDUE, "last_success_at": shown}
    if integrity_view(status, now=now)["status"] == STALE:
        return {"status": STALE, "last_success_at": shown}
    state = backup.get("state")
    if not state:
        return {"status": PENDING, "last_success_at": shown}
    return {"status": HEALTHY if state == "ok" else FAILED, "last_success_at": shown}


def guardian_health(database_url: str) -> dict[str, object]:
    """What /health shows. A backup problem never changes status: it mustn't block the promote that may fix it."""
    try:
        paths = paths_from_database_url(database_url)
    except BackupValidationError:
        return {"status": "ok", "database_integrity": None, "database_backup": None}
    status = read_status(status_path(paths.database))
    integrity = integrity_view(status)
    return {"status": overall_status(integrity), "database_integrity": integrity, "database_backup": backup_view(status)}


def publish_integrity_alert(state: str, *, session_factory=None) -> None:
    """Critical alert to every platform admin, on the same path as a backup failure."""
    from sqlalchemy import text
    from modules.notifications.alert_dispatcher import deliver_staged_alert
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    with session_factory() as db:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL, no user input
        admins = db.execute(text("SELECT id FROM users WHERE role='admin' AND group_id IS NULL AND is_active IS TRUE")).scalars().all()
        plan = stage_admin_alert(db, ALERT_TITLE, _ALERT_MESSAGES[state], {"kind": "database_integrity", "state": state}, admins)
        db.commit()
        deliver_staged_alert(db, plan)


def alert_if_stopped(database_url: str, *, session_factory=None, now: datetime | None = None) -> bool:
    """For another process to call: a stopped guardian can't raise its own alert, or back up."""
    view = database_integrity(database_url)
    if not view or view["status"] != STALE:
        return False
    from sqlalchemy import select
    from modules.notifications.models import Alert
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    now = now or datetime.now(timezone.utc)
    alerts = Alert.__table__
    with session_factory() as db:
        recent = db.execute(select(alerts.c.metadata_json).where(
            alerts.c.title == ALERT_TITLE, alerts.c.created_at > now - ALERT_REPEAT)).scalars().all()
    # A damage alert says something else: it doesn't stand in for "nothing is checking any more".
    if any(isinstance(item, dict) and item.get("state") == UNAVAILABLE for item in recent):
        return False
    publish_integrity_alert(UNAVAILABLE, session_factory=session_factory)
    return True
