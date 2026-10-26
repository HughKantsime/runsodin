"""One immediate attention alert per physical attempt/kind/code."""
import hashlib
import json
from sqlalchemy import text
from core.base import AlertType, AlertSeverity


def stage(db, item):
    if item['kind'] not in ('paused', 'runout', 'failed') or not item.get('attempt'):
        return None
    # Education generic notifications remain suppressed.
    if db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'), {'id': item['job']}).first():
        return None
    from modules.printers.start_next import insert
    identity = [item['attempt'], item['kind'], item.get('code')]
    key = 'job_fault:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    if not insert(db.connection(), key, {'sent': True}):
        return None
    # Failure is already the immediate, durable print_failed terminal summary.
    if item['kind'] == 'failed':
        return None
    from modules.notifications.alert_dispatcher import stage_alert
    return stage_alert(db, AlertType.PRINTER_ERROR, AlertSeverity.WARNING,
                       'Print needs attention', 'Print paused' if item['kind'] == 'paused' else 'Filament runout',
                       printer_id=item['printer'], job_id=item['job'],
                       metadata={'fault': {'print_attempt_uid': item['attempt'], 'kind': item['kind'], 'code': item.get('code')}})


def notify(printer_id, kind, code=None):
    """Separate attention lane: timeline backpressure cannot discard a fault.

    Telemetry callers submit without SQL or network I/O. Durable tuple claiming
    happens on the attention worker's private connection before delivery.
    """
    try:
        _worker.submit(_notify, printer_id, kind, code)
    except RuntimeError:
        # Shutdown cannot escape into a telemetry producer.
        return False
    return True


def _notify(printer_id, kind, code):
    try:
        from core.db import current_engine
        from sqlalchemy.orm import Session
        from modules.notifications.alert_dispatcher import _deliver_staged_alert
        from modules.notifications.alert_outbox import deliver_mobile
        engine = current_engine()
        with Session(engine) as db:
            row = db.execute(text("SELECT scheduled_job_id,economic_uid FROM print_jobs WHERE printer_id=:id AND status IN ('running','printing','paused') ORDER BY id DESC LIMIT 2"), {'id': printer_id}).all()
            if len(row) != 1 or row[0][0] is None or row[0][1] is None:
                return
            plan = stage(db, dict(job=row[0][0], attempt=row[0][1], printer=printer_id, kind=kind, code=code))
            db.commit()
            if plan is not None:
                _deliver_staged_alert(db,plan)
                deliver_mobile(db,plan)
    except Exception:
        # The durable writer also attempts the same tuple. Neither path can
        # break telemetry, and tuple ownership prevents duplicate fanout.
        import logging
        logging.getLogger('odin.job_faults').warning('Attention delivery deferred to durable history')


from concurrent.futures import ThreadPoolExecutor
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='job-attention')
