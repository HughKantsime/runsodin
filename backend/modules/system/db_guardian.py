"""Supervised database guardian: scheduled integrity checks and daily verified backups.

It only reads the live database, through a read-only connection and VACUUM INTO, and writes to
it only to raise an alert. It never repairs anything. Damage stays visible on /health, which
reads the status file here, and raises a critical alert. A later check clears them only if a
person has fixed the database.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.db_integrity import DAMAGED, OK, RUNBOOK, UNAVAILABLE, IntegrityReport, check_database
from modules.system import backup_failure_alerts
from modules.system.backup_failure_alerts import backup_failure_reason
from modules.system.backup_schedule import create_scheduled_backup
from modules.system.backup_service import (
    BackupMissedError,
    BackupValidationError,
    SourceDatabaseError,
    paths_from_database_url,
)
from modules.system.db_guardian_status import (
    STALE,
    database_integrity,
    integrity_view,
    offsite_path,
    parse_time,
    read_status,
    status_path,
    write_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [db_guardian] %(levelname)s %(message)s")
log = logging.getLogger("odin.db_guardian")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
# Measured with SQLite 3.51.3 on two cores of a test VM: 0.17 s on a 315 MB database shaped like
# production's, 2.5 s with every page live and a cold cache. Writers at 30 commits/s saw no BUSY
# and no worse latency with checks running back to back. The VM reads about 5 times faster than
# production, so expect about 1 s there, 15 s at the worst: at most 5% of one low-priority core.
# This is also the longest ODIN can write on damage unnoticed.
INTEGRITY_INTERVAL_SECONDS = 300
BACKUP_INTERVAL = timedelta(days=1)
BACKUP_RETRY = timedelta(hours=1)
BACKUP_MISSED_AFTER = timedelta(hours=26)
OFFSITE_MISSED_AFTER = timedelta(hours=36)
# Boot has just made this check, twice; let the services settle before the next one.
FIRST_CHECK_DELAY_SECONDS = 60
ALERT_REPEAT = timedelta(days=1)
# One check that can't finish is usually a passing lock; three in a row means nothing is checking.
UNAVAILABLE_ALERT_AFTER = 3
TICK_SECONDS = 15
# Below normal priority: on a two-core host the API and monitors come first.
NICE_INCREMENT = 10

ALERT_TITLE = "Database integrity check failed"
_ALERT_MESSAGES = {
    DAMAGED: "ODIN's database failed its integrity check. ODIN won't repair it, and a restart "
             "will refuse to start until it is fixed. Follow the runbook before restarting: " + RUNBOOK,
    UNAVAILABLE: "ODIN's scheduled database integrity check has not been able to finish. The database "
                 "may be fine, but nothing is checking it. See the runbook: " + RUNBOOK,
}


def publish_integrity_alert(state: str, *, session_factory=None) -> None:
    """Critical alert to every platform admin, through the normal path so it can reach a phone.

    Reuses the maintenance classification, as backup failures do; a new alert type would be a
    schema change. A muted preference still gets the inbox row: this isn't a routine alert.
    """
    from sqlalchemy import text
    from core.base import AlertSeverity, AlertType
    from modules.notifications.alert_dispatcher import _deliver_in_app, deliver_staged_alert, stage_alert
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    message = _ALERT_MESSAGES[state]
    metadata = {"kind": "database_integrity", "state": state}
    with session_factory() as db:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL, no user input
        admins = db.execute(text("SELECT id FROM users WHERE role='admin' AND group_id IS NULL AND is_active IS TRUE")).scalars().all()
        plan = stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, ALERT_TITLE, message,
                           metadata=metadata, target_user_ids=list(admins))
        covered = set(plan.in_app_users) if plan else set()
        for user_id in admins:
            if user_id not in covered:
                _deliver_in_app(db, user_id, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL,
                                ALERT_TITLE, message, None, None, None, metadata)
        db.commit()
        deliver_staged_alert(db, plan)


def record_integrity(status: dict, report: IntegrityReport, now: datetime, *, publish=publish_integrity_alert) -> None:
    """Record a result, then alert if it calls for one. An alert that fails to send doesn't change /health."""
    previous = status.get("integrity") if isinstance(status.get("integrity"), dict) else {}
    misses = int(previous.get("consecutive_unavailable") or 0) + 1 if report.state == UNAVAILABLE else 0
    alerted_at = previous.get("alerted_at") if report.state != OK else None
    # A result that can't say anything about the data doesn't overwrite known damage.
    keep_damage = report.state == UNAVAILABLE and previous.get("state") == DAMAGED
    status["integrity"] = {
        "state": DAMAGED if keep_damage else report.state,
        "checked_at": previous.get("checked_at") if keep_damage else now.isoformat(),
        "seconds": round(report.seconds, 3),
        "consecutive_unavailable": misses,
        "alerted_at": alerted_at,
    }
    if report.state == OK:
        return
    log.error("Integrity check result %s: %d problem(s), %d foreign key violation(s), first: %s",
              report.state, report.integrity_problems, report.foreign_key_violations, list(report.problems)[:3])
    alert_state = DAMAGED if keep_damage else report.state
    if alert_state == UNAVAILABLE and misses < UNAVAILABLE_ALERT_AFTER:
        return
    last_alert = parse_time(alerted_at)
    if last_alert is not None and now - last_alert < ALERT_REPEAT:
        return
    try:
        publish(alert_state)
    except Exception as exc:
        # The damaged page may be in the alerts table. /health still reports it; try again next check.
        log.error("Integrity alert could not be saved (%s)", type(exc).__name__)
        return
    status["integrity"]["alerted_at"] = now.isoformat()


def _run_backup(status: dict, now: datetime, database_url: str, *, create=create_scheduled_backup,
                publish=publish_integrity_alert) -> None:
    backup = status.get("backup") if isinstance(status.get("backup"), dict) else {}
    backup["last_attempt_at"] = now.isoformat()
    status["backup"] = backup
    try:
        result = create(database_url, now=now)
    except SourceDatabaseError as error:
        reason, _ = backup_failure_reason(error)
        backup.update(state="refused", reason=reason)
        log.error("Scheduled backup refused (%s)", reason)
        record_integrity(status, error.report, now, publish=publish)
    except Exception as error:
        reason, _ = backup_failure_reason(error)
        backup.update(state="failed", reason=reason)
        log.error("Scheduled backup failed (%s)", reason)
    else:
        backup.update(state="ok", reason=None, last_success_at=now.isoformat(), filename=result["filename"],
                      size_bytes=result["size_bytes"])
        log.info("Scheduled backup %s verified, %d bytes, pruned %d", result["filename"], result["size_bytes"], len(result["pruned"]))
        # That run's first step was a full check of the live database; no need to repeat it now.
        record_integrity(status, result["source_report"], now, publish=publish)


def _backup_due(status: dict, now: datetime) -> bool:
    backup = status.get("backup") if isinstance(status.get("backup"), dict) else {}
    last_success = parse_time(backup.get("last_success_at"))
    last_attempt = parse_time(backup.get("last_attempt_at"))
    if last_attempt is not None and now - last_attempt < BACKUP_RETRY:
        return False
    return last_success is None or now - last_success >= BACKUP_INTERVAL


def _integrity_due(status: dict, now: datetime, interval: int) -> bool:
    integrity = status.get("integrity") if isinstance(status.get("integrity"), dict) else {}
    checked_at = parse_time(integrity.get("checked_at"))
    return checked_at is None or (now - checked_at).total_seconds() >= interval


def missed_backups(status: dict, now: datetime, database: Path) -> list[BackupMissedError]:
    """What should exist by now and doesn't. Off-server copies only count once one has been confirmed."""
    backup = status.get("backup") if isinstance(status.get("backup"), dict) else {}
    missed = []
    last_local = parse_time(backup.get("last_success_at")) or parse_time(status.get("started_at"))
    if last_local is not None and now - last_local >= BACKUP_MISSED_AFTER:
        missed.append(BackupMissedError("missed"))
    confirmed = parse_time(read_status(offsite_path(database)).get("confirmed_at"))
    if confirmed is not None and now - confirmed >= OFFSITE_MISSED_AFTER:
        missed.append(BackupMissedError("offsite_missed"))
    return missed


def _alert_missed(status: dict, now: datetime, database: Path) -> None:
    for error in missed_backups(status, now, database):
        log.error("Backup missed (%s)", error.reason)
        try:
            # Saved once per reason per day, like every other backup failure.
            backup_failure_alerts.publish_backup_failure(error)
        except Exception as exc:
            log.error("Missed backup alert could not be saved (%s)", type(exc).__name__)


def tick(status: dict, now: datetime, database: Path, database_url: str, *, interval: int = INTEGRITY_INTERVAL_SECONDS,
         check=check_database, create=create_scheduled_backup, publish=publish_integrity_alert) -> bool:
    """Do what is due. Returns whether the status changed."""
    if not _integrity_due(status, now, interval):
        return False
    status["interval_seconds"] = interval
    if _backup_due(status, now):
        _run_backup(status, now, database_url, create=create, publish=publish)
    else:
        record_integrity(status, check(database), now, publish=publish)
    _alert_missed(status, now, database)
    return True


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
        recent = db.execute(select(alerts.c.id).where(
            alerts.c.title == ALERT_TITLE, alerts.c.created_at > now - ALERT_REPEAT)).first()
    if recent is not None:
        return False
    publish_integrity_alert(UNAVAILABLE, session_factory=session_factory)
    return True


def main_loop() -> None:
    try:
        paths = paths_from_database_url(DATABASE_URL)
    except BackupValidationError:
        log.info("The database guardian covers SQLite only; idle on this database")
        while True:
            time.sleep(3600)
    os.nice(NICE_INCREMENT)
    path = status_path(paths.database)
    status = read_status(path)
    # A restart keeps the results and the schedule: a crash loop must not turn into a check loop.
    status.update(schema_version=1, interval_seconds=INTEGRITY_INTERVAL_SECONDS,
                  started_at=status.get("started_at") or datetime.now(timezone.utc).isoformat())
    write_status(path, status)
    log.info("Database guardian started; integrity check every %d s, backup every %s",
             INTEGRITY_INTERVAL_SECONDS, BACKUP_INTERVAL)
    # After a long outage /health says stale until a check finishes, so don't make it wait.
    if integrity_view(status)["status"] != STALE:
        time.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            if tick(status, datetime.now(timezone.utc), paths.database, DATABASE_URL):
                write_status(path, status)
        except Exception as exc:
            log.error("Guardian cycle failed (%s)", type(exc).__name__)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    main_loop()
