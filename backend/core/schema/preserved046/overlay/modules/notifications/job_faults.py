"""Durable pause episodes and immediate filament-runout attention alerts."""
import hashlib
import json
from sqlalchemy import text
from core.base import AlertType, AlertSeverity


def _pause_claim(db, item):
    """Serialize episode state across telemetry lanes and daemon restarts."""
    from core.durable_config import insert
    from datetime import datetime, timezone
    key = 'pause_episode:' + str(item['printer'])
    insert(db.connection(), key, {})
    # SQLite takes its writer lock; PostgreSQL locks this printer's row.
    db.execute(text('UPDATE system_config SET value=value WHERE key=:key'), {'key': key})
    state = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).scalar_one()
    while isinstance(state, str):
        state = json.loads(state)
    occurred = item.get('occurred') or datetime.now(timezone.utc)
    stamp = occurred.isoformat() if hasattr(occurred, 'isoformat') else str(occurred)
    if state.get('at', '') > stamp:
        return False
    paused = item['kind'] == 'paused'
    claimed = paused and (not state.get('paused') or state.get('attempt') != item.get('attempt'))
    # Missing physical identity can become known during a pre-print pause. That
    # does not constitute a second episode.
    if paused and state.get('paused') and (not state.get('attempt') or not item.get('attempt')):
        claimed = False
    from sqlalchemy import bindparam, JSON
    stmt = text('UPDATE system_config SET value=:value WHERE key=:key')
    if db.get_bind().dialect.name == 'postgresql':
        stmt = stmt.bindparams(bindparam('value', type_=JSON))
    attempt = item.get('attempt') if claimed or not paused else item.get('attempt') or state.get('attempt')
    db.execute(stmt, {'key': key, 'value': json.dumps({'paused': paused, 'at': stamp, 'attempt': attempt})})
    return claimed


def stage(db, item):
    kind = item['kind']
    if kind in ('started', 'resumed', 'completed', 'cancelled', 'failed'):
        _pause_claim(db, item)
        return None
    if kind not in ('paused', 'runout'):
        return None
    from core.interfaces.education_policy import is_education_reserved_filename
    if is_education_reserved_filename(item.get('context', {}).get('filename')):
        return None
    from core.recipient_policy import education_notification_scope as education_scope
    # Query/classification failures propagate, so no generic delivery escapes.
    if education_scope(db, job_id=item.get('job'), printer_id=item['printer']):
        return None
    if kind == 'paused':
        if not _pause_claim(db, item):
            return None
    elif item.get('attempt'):
        from core.durable_config import insert
        identity = [item.get('attempt') or item['printer'], kind, item.get('code')]
        key = 'job_fault:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if not insert(db.connection(), key, {'sent': True}):
            return None
    from modules.notifications.alert_dispatcher import stage_alert
    return stage_alert(db, AlertType.PRINT_PAUSED if kind == 'paused' else AlertType.PRINTER_ERROR,
                       AlertSeverity.CRITICAL if kind == 'paused' else AlertSeverity.WARNING,
                       'Print needs attention', 'Print paused' if kind == 'paused' else 'Filament runout',
                       printer_id=item['printer'], job_id=item.get('job'),
                       metadata={**item.get('context', {}), 'code': item.get('code'),
                                 'fault': {'print_attempt_uid': item.get('attempt'), 'kind': kind, 'code': item.get('code')}})


def notify(printer_id, kind, code=None, *, occurred=None, context=None):
    """Separate attention lane: timeline backpressure cannot discard a fault.

    Telemetry callers submit without SQL or network I/O. Durable tuple claiming
    happens on the attention worker's private connection before delivery.
    """
    try:
        _worker.submit(_notify, printer_id, kind, code, occurred=occurred, context=context)
    except RuntimeError:
        # Shutdown cannot escape into a telemetry producer.
        return False
    return True


def _notify(printer_id, kind, code, *, occurred=None, context=None):
    try:
        from core.db import current_engine
        from sqlalchemy.orm import Session
        from modules.notifications.alert_dispatcher import _deliver_staged_alert
        from core.interfaces.education_policy import is_education_reserved_filename
        engine = current_engine()
        with Session(engine) as db:
            rows = db.execute(text("SELECT scheduled_job_id,economic_uid,filename FROM print_jobs WHERE printer_id=:id AND status IN ('running','printing','paused') ORDER BY id DESC LIMIT 2"), {'id': printer_id}).all()
            if len(rows) > 1:
                return
            row = rows[0] if rows else None
            if row and is_education_reserved_filename(row[2]):
                return
            if not db.execute(text('SELECT 1 FROM printers WHERE id=:id'), {'id': printer_id}).first():
                return
            plan = stage(db, dict(job=row[0] if row else None, attempt=row[1] if row else None,
                                  printer=printer_id, kind=kind, code=code, occurred=occurred, context=context or {}))
            db.commit()
            if plan is not None:
                _deliver_staged_alert(db, plan)
    except Exception as exc:
        import logging
        logging.getLogger('odin.job_faults').warning('Attention delivery deferred to durable history (%s)', type(exc).__name__)


from concurrent.futures import ThreadPoolExecutor
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='job-attention')
