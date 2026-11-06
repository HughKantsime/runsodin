"""Daily persisted verification and restart-safe SIEM sweeper for the API lifespan."""
import asyncio
import json
import logging
import time
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.audit_integrity import verify
from core.audit_receipts import seal, authentic

log = logging.getLogger('odin.audit_integrity')


def health(engine):
    from license_manager import get_license
    if not get_license().has_feature('audit_integrity'):
        return {'status': 'not_licensed'}
    try:
        with engine.connect() as connection:
            from core.audit_keys import missing_key
            if missing_key(connection):
                return {'status': 'audit_key_missing'}
            row = connection.execute(text('SELECT checked_at,result FROM audit_integrity_status WHERE id=1')).first()
        if row is None:
            from core.audit_keys import saved_head
            return {'status': 'attention' if saved_head() else 'pending'}
        from core.audit_keys import saved_head
        result = json.loads(row[1])
        if not authentic(saved_head()) or not authentic(result) or result.get('checked_at') != row[0]:
            return {'status': 'attention'}
        return {'status': ('ok' if result['ok'] else 'critical') if time.time()-row[0] <= 90000 else 'stale',
                'checked_at': row[0], 'first_break': result.get('first_break')}
    except Exception:
        return {'status': 'attention'}


def _check_daily(engine, *, now=None, force=False):
    from license_manager import get_license
    if not get_license().has_feature('audit_integrity'):
        return None
    now = time.time() if now is None else now
    from core.audit_snapshot import snapshot
    with snapshot(engine) as connection:
        from core.audit_keys import missing_key
        if missing_key(connection):
            return verify(connection)
        prior = connection.execute(text('SELECT checked_at,result FROM audit_integrity_status WHERE id=1')).first()
        try:
            previous = json.loads(prior[1]) if prior else None
        except (ValueError, TypeError):
            previous = None
        if not isinstance(previous, dict):
            previous = None
        trusted = previous and authentic(previous) and previous.get('checked_at') == prior[0]
        if trusted and now-prior[0] < 86400 and not force:
            return previous
        full_at = previous.get('full_checked_at', 0) if previous else 0
        incremental = bool(trusted and previous['ok'] and now-full_at < 7*86400 and not force)
        if incremental:
            # An erasure rewrites earlier rows; inspect them with a full snapshot.
            incremental = not connection.execute(text("SELECT 1 FROM audit_logs WHERE chain_seq>:seq "
                "AND action IN ('audit.erasure','audit.retention') LIMIT 1"), {'seq': previous['rows']}).first()
        result = verify(connection, start=previous if incremental else None)
        result['full_checked_at'] = full_at if incremental else now
        result['incremental'] = bool(incremental)
        result['checked_at'] = now
        result = seal(result, connection)
    with Session(engine) as db:
        recorded = {b.get('fingerprint') for b in previous.get('breaks', [])} if trusted else set()
        if any(b['fingerprint'] not in recorded for b in result['breaks']):
            from core.base import AlertType, AlertSeverity
            from modules.notifications.models import Alert
            for user_id in db.execute(text("SELECT id FROM users WHERE role='admin' AND group_id IS NULL AND is_active IS TRUE")).scalars():
                db.add(Alert(user_id=user_id, alert_type=AlertType.AUDIT_INTEGRITY, severity=AlertSeverity.CRITICAL,
                             title='Audit integrity failure', message='Audit chain verification failed. Preserve the database and investigate.',
                             metadata_json={'audit_integrity': result}))
        db.execute(text('''INSERT INTO audit_integrity_status(id,checked_at,result) VALUES(1,:now,:result)
            ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at,result=excluded.result'''),
                   {'now': now, 'result': json.dumps(result)})
        db.commit()
        from core.audit_heads import advance_head
        advance_head(result)
        return result


def check_daily(engine, *, now=None, force=False):
    from core.audit_keys import file_lock
    # Installation-wide advisory file lock: shared by all local API workers,
    # independent of audit appends and held across snapshot + status persistence.
    with file_lock('.audit-daily.lock', blocking=False) as acquired:
        if not acquired:
            return None
        return _check_daily(engine, now=now, force=force)


async def run():
    from core.db import current_engine
    from modules.system.audit_siem import deliver_batch, licensed_destinations
    workers = {}

    async def worker(destination):
        while True:
            try:
                await asyncio.to_thread(deliver_batch, current_engine(), destination)
            except Exception as exc:
                log.error('SIEM worker unavailable (%s)', type(exc).__name__)
            await asyncio.sleep(1)

    try:
        while True:
            try:
                destinations = set(await asyncio.to_thread(licensed_destinations, current_engine()))
                for destination in destinations - workers.keys():
                    workers[destination] = asyncio.create_task(worker(destination))
                for destination in workers.keys() - destinations:
                    workers.pop(destination).cancel()
                await asyncio.to_thread(check_daily, current_engine())
            except Exception as exc:
                log.error('Audit maintenance unavailable (%s)', type(exc).__name__)
            await asyncio.sleep(10)
    finally:
        for task in workers.values():
            task.cancel()
        await asyncio.gather(*workers.values(), return_exceptions=True)
