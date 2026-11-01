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

from core.db_integrity import DAMAGED, OK, UNAVAILABLE, IntegrityReport, check_database
from modules.system import backup_failure_alerts
from modules.system.backup_failure_alerts import backup_failure_reason
from modules.system.backup_schedule import clear_leftovers, create_scheduled_backup
from modules.system.backup_service import (
    BackupMissedError,
    BackupValidationError,
    SourceDatabaseError,
    paths_from_database_url,
)
from modules.system.db_guardian_status import (
    ALERT_REPEAT,
    BACKUP_MISSED_AFTER_SECONDS,
    STALE,
    UNAVAILABLE_ALERT_AFTER,
    integrity_view,
    offsite_path,
    parse_time,
    publish_integrity_alert,
    read_status,
    status_path,
    write_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [db_guardian] %(levelname)s %(message)s")
log = logging.getLogger("odin.db_guardian")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
# Measured with SQLite 3.51.3 on two cores of a test VM: 0.17 s on a 315 MB database shaped like
# production's, 2.5 s with every page live and a cold cache. Writers at 30 commits/s saw no BUSY
# and no worse latency with checks running back to back. On a disk throttled to 300 IOPS a cold
# check took 26 s, about 9% of one low-priority core at this interval; production's speed isn't
# measured. This is also the longest ODIN can write on damage unnoticed.
INTEGRITY_INTERVAL_SECONDS = 300
BACKUP_INTERVAL = timedelta(days=1)
BACKUP_RETRY = timedelta(hours=1)
BACKUP_MISSED_AFTER = timedelta(seconds=BACKUP_MISSED_AFTER_SECONDS)
OFFSITE_MISSED_AFTER = timedelta(hours=36)
# The job runs hourly and alerts by itself after three failures; after an outage, give it that long.
OFFSITE_BOOT_GRACE = timedelta(hours=4)
# But not for ever: every restart restarts the grace, so a restart loop must not hold the alert for good.
OFFSITE_GRACE_LIMIT = timedelta(days=3)
# Boot has just made this check, twice; let the services settle before the next one.
FIRST_CHECK_DELAY_SECONDS = 60
TICK_SECONDS = 15
# Below normal priority: on a two-core host the API and monitors come first.
NICE_INCREMENT = 10

def record_integrity(status: dict, report: IntegrityReport, now: datetime, *, publish=publish_integrity_alert) -> None:
    """Record a result, then alert if it calls for one. An alert that fails to send doesn't change /health."""
    previous = status.get("integrity") if isinstance(status.get("integrity"), dict) else {}
    misses = int(previous.get("consecutive_unavailable") or 0) + 1 if report.state == UNAVAILABLE else 0
    # Each state has its own daily limit: "may be fine" must never hold back real damage.
    alerted = previous.get("alerted") if isinstance(previous.get("alerted"), dict) else {}
    alerted = {} if report.state == OK else dict(alerted)
    # A result that can't say anything about the data doesn't overwrite known damage.
    keep_damage = report.state == UNAVAILABLE and previous.get("state") == DAMAGED
    status["integrity"] = {
        "state": DAMAGED if keep_damage else report.state,
        "checked_at": previous.get("checked_at") if keep_damage else now.isoformat(),
        "attempted_at": now.isoformat(),
        "seconds": round(report.seconds, 3),
        "consecutive_unavailable": misses,
        "alerted": alerted,
    }
    if report.state == OK:
        return
    log.error("Integrity check result %s: %d problem(s), %d foreign key violation(s), first: %s",
              report.state, report.integrity_problems, report.foreign_key_violations, list(report.problems)[:3])
    alert_state = DAMAGED if keep_damage else report.state
    if alert_state == UNAVAILABLE and misses < UNAVAILABLE_ALERT_AFTER:
        return
    last_alert = parse_time(alerted.get(alert_state))
    if last_alert is not None and now - last_alert < ALERT_REPEAT:
        return
    try:
        publish(alert_state)
    except Exception as exc:
        # The damaged page may be in the alerts table. /health still reports it; try again next check.
        log.error("Integrity alert could not be saved (%s)", type(exc).__name__)
        return
    alerted[alert_state] = now.isoformat()


def _run_backup(status: dict, now: datetime, database_url: str, *, create=create_scheduled_backup,
                publish=publish_integrity_alert, save=lambda _status: None) -> None:
    backup = status.get("backup") if isinstance(status.get("backup"), dict) else {}
    backup["last_attempt_at"] = now.isoformat()
    status["backup"] = backup
    # Before starting: a run killed halfway mustn't start again straight after the restart.
    save(status)
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
    # attempted_at, not checked_at: with damage known, checked_at stays put while checks can't finish.
    last_run = parse_time(integrity.get("attempted_at")) or parse_time(integrity.get("checked_at"))
    return last_run is None or (now - last_run).total_seconds() >= interval


def missed_backups(status: dict, now: datetime, database: Path) -> list[BackupMissedError]:
    """What should exist by now and doesn't. Off-server copies only count once the job has registered."""
    backup = status.get("backup") if isinstance(status.get("backup"), dict) else {}
    missed = []
    last_local = parse_time(backup.get("last_success_at")) or parse_time(status.get("started_at"))
    if last_local is not None and now - last_local >= BACKUP_MISSED_AFTER:
        missed.append(BackupMissedError("missed"))
    offsite = read_status(offsite_path(database))
    last_offsite = parse_time(offsite.get("confirmed_at")) or parse_time(offsite.get("expected_since"))
    booted = parse_time(status.get("booted_at"))
    if last_offsite is not None and now - last_offsite >= OFFSITE_MISSED_AFTER:
        settled = booted is None or now - booted >= OFFSITE_BOOT_GRACE
        if settled or now - last_offsite >= OFFSITE_GRACE_LIMIT:
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
         check=check_database, create=create_scheduled_backup, publish=publish_integrity_alert,
         save=lambda _status: None) -> bool:
    """Do what is due. Returns whether the status changed."""
    if not _integrity_due(status, now, interval):
        return False
    status["interval_seconds"] = interval
    if _backup_due(status, now):
        _run_backup(status, now, database_url, create=create, publish=publish, save=save)
    else:
        record_integrity(status, check(database), now, publish=publish)
    _alert_missed(status, now, database)
    return True


def _save_status(path: Path, status: dict) -> None:
    """A full disk or an unwritable directory is no reason to stop checking. /health goes stale by itself."""
    try:
        write_status(path, status)
    except OSError as exc:
        log.error("Status could not be saved (%s)", type(exc).__name__)


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
    now = datetime.now(timezone.utc).isoformat()
    # A restart keeps the results and the schedule: a crash loop must not turn into a check loop.
    status.update(schema_version=1, interval_seconds=INTEGRITY_INTERVAL_SECONDS, booted_at=now,
                  started_at=status.get("started_at") or now)
    _save_status(path, status)
    try:
        clear_leftovers(DATABASE_URL)
    except OSError as exc:
        log.error("A killed backup's leftovers could not be cleared (%s)", type(exc).__name__)
    log.info("Database guardian started; integrity check every %d s, backup every %s",
             INTEGRITY_INTERVAL_SECONDS, BACKUP_INTERVAL)
    # After a long outage /health says stale until a check finishes, so don't make it wait.
    if integrity_view(status)["status"] != STALE:
        time.sleep(FIRST_CHECK_DELAY_SECONDS)
    while True:
        try:
            if tick(status, datetime.now(timezone.utc), paths.database, DATABASE_URL,
                    save=lambda current: _save_status(path, current)):
                _save_status(path, status)
        except Exception as exc:
            log.error("Guardian cycle failed (%s)", type(exc).__name__)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.schema.bootstrap import configure_orm
    configure_orm()
    main_loop()
