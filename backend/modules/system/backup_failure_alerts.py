"""Persistent, admin-only backup failure alerts, without exception text or paths."""
import errno
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from sqlalchemy import func, select, text

BACKUP_ALERT_TITLE = 'Database backup failed'


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


def stage_admin_alert(db, title, message, metadata, admins):
    """Critical alert to these admins through the normal path, so it can reach a phone.

    The caller commits, then delivers with deliver_staged_alert. Reuses the maintenance
    classification; a new alert type would be a schema change. A muted preference still gets the
    inbox row: these aren't routine alerts.
    """
    from core.base import AlertSeverity, AlertType
    from modules.notifications.alert_dispatcher import _deliver_in_app, stage_alert
    plan = stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, title, message,
                       metadata=metadata, target_user_ids=list(admins))
    covered = set(plan.in_app_users) if plan else set()
    for user_id in admins:
        if user_id not in covered:
            _deliver_in_app(db, user_id, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL,
                            title, message, None, None, None, metadata)
    return plan


def publish_backup_failure(error, *, session_factory=None, now=None):
    from modules.notifications.alert_dispatcher import deliver_staged_alert
    from modules.notifications.models import Alert
    from modules.system.backup_service import restore_lock
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    reason, message = backup_failure_reason(error)
    now = now or datetime.now(timezone.utc)
    alerts = Alert.__table__
    plan = None
    with session_factory() as db:
        # A separate lock avoids reacquiring the restore lock held by safety backups.
        # The persisted alert query survives restarts and includes read/dismissed alerts.
        with restore_lock(Path(tempfile.gettempdir()) / 'odin-backup-failure-alerts.lock'):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL, no user input
            admins = db.execute(text("SELECT id FROM users WHERE role='admin' AND group_id IS NULL AND is_active IS TRUE")).scalars().all()
            pending = []
            for user_id in admins:
                existing = db.execute(select(alerts.c.metadata_json).where(
                    alerts.c.user_id == user_id,
                    alerts.c.title == BACKUP_ALERT_TITLE,
                    alerts.c.created_at > now - timedelta(days=1),
                )).scalars().all()
                if not any(isinstance(item, dict) and item.get('reason') == reason for item in existing):
                    pending.append(user_id)
            if pending:
                newest = db.execute(select(func.max(alerts.c.id))).scalar() or 0
                plan = stage_admin_alert(db, BACKUP_ALERT_TITLE, message, {'kind': 'backup_failed', 'reason': reason}, pending)
                db.flush()
                db.execute(alerts.update().where(alerts.c.id > newest, alerts.c.title == BACKUP_ALERT_TITLE).values(created_at=now))
            db.commit()
        # After the lock: it can wait on the network, and the row is already safe.
        if plan is not None:
            deliver_staged_alert(db, plan)


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
