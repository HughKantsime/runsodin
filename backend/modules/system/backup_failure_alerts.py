"""Persistent, admin-only backup failure alerts, without exception text or paths."""
import errno
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from sqlalchemy import select, text


_MISSED_MESSAGES = {
    'missed': 'Backup missed: ODIN has made no verified backup in over 26 hours.',
    'offsite_missed': 'Backup missed: no copy has been confirmed off this server in over 36 hours.',
}


def backup_failure_reason(error):
    from modules.system.backup_service import BackupMissedError, BackupValidationError, SourceDatabaseError
    if isinstance(error, BackupMissedError):
        return error.reason, _MISSED_MESSAGES[error.reason]
    if isinstance(error, SourceDatabaseError):
        if error.report.state == 'damaged':
            return 'source_integrity', 'Backup refused: the live database failed its integrity check. Existing backups were kept.'
        return 'source_unavailable', 'Backup refused: the live database could not be checked. Existing backups were kept.'
    message = str(error).lower()
    if 'size' in message or 'too large' in message:
        return 'size', 'Backup refused: file size is outside the supported range (100 bytes to 1 GiB).'
    if 'space' in message or isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return 'disk_space', 'Backup refused: insufficient free disk space for a verified backup.'
    if 'restore test' in message:
        return 'restore_test', 'Backup refused: a copy of it failed the restore test. Existing backups were kept.'
    if isinstance(error, BackupValidationError):
        return 'validation', 'Backup refused: database validation failed.'
    return 'operation', 'Backup failed: the database snapshot could not be created.'


def publish_backup_failure(error, *, session_factory=None, now=None):
    from core.base import AlertType, AlertSeverity
    from modules.notifications.models import Alert
    from modules.notifications.alert_dispatcher import _deliver_in_app
    from modules.system.backup_service import restore_lock
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    reason, message = backup_failure_reason(error)
    now = now or datetime.now(timezone.utc)
    alerts = Alert.__table__
    # A separate lock avoids reacquiring the restore lock held by safety backups.
    # The persisted alert query survives restarts and includes read/dismissed alerts.
    with restore_lock(Path(tempfile.gettempdir()) / 'odin-backup-failure-alerts.lock'):
        with session_factory() as db:
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL, no user input
            admins = db.execute(text("SELECT id FROM users WHERE role='admin' AND group_id IS NULL AND is_active IS TRUE")).scalars().all()
            for user_id in admins:
                existing = db.execute(select(alerts.c.metadata_json).where(
                    alerts.c.user_id == user_id,
                    alerts.c.title == 'Database backup failed',
                    alerts.c.created_at > now - timedelta(days=1),
                )).scalars().all()
                if any(isinstance(item, dict) and item.get('reason') == reason for item in existing):
                    continue
                # Reuse the existing maintenance alert classification and inbox.
                alert = _deliver_in_app(
                    db, user_id, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL,
                    'Database backup failed', message, None, None, None,
                    {'kind': 'backup_failed', 'reason': reason})
                alert.created_at = now
            db.commit()


def report_backup_failure(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as error:
            try:
                publish_backup_failure(error)
            except Exception:
                logging.getLogger(__name__).warning('Backup failure alert could not be persisted')
            raise
    return checked
