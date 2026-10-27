"""Bounded one-shot dispatch workers. No replay, restart recovery or retries.

The worker keeps its target lock from claim through cleanup. A request is
acknowledged at claim commit or with a durable handle if staging times out.
A process crash leaves that claim for explicit reconciliation.
"""
from contextvars import ContextVar, copy_context
from threading import Event, Thread, BoundedSemaphore, Lock, current_thread
import time
import uuid
from fastapi import HTTPException
from modules.printers.dispatch_handle import status_url
from sqlalchemy import text
from core.durable_config import insert
from modules.printers.start_next import load, save

_context = ContextVar('background_dispatch', default=None)
_slots = BoundedSemaphore(8)
_workers = set()
_workers_lock = Lock()
_draining = False
STAGING_SECONDS = 30


def drain(timeout=30):
    """Stop admission and allow current workers one shared bounded grace period."""
    global _draining
    deadline = time.monotonic() + timeout
    with _workers_lock:
        _draining = True
        workers = tuple(_workers)
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))
    return all(not worker.is_alive() for worker in workers)


def startup():
    global _draining
    with _workers_lock:
        _draining = False


def attempt_token():
    state = _context.get()
    return state.get('token') if state else None


def active(token):
    with _workers_lock:
        return any(getattr(worker, 'attempt_id', None) == token for worker in _workers)


def result_key(job_id):
    return 'dispatch_result:' + str(job_id)


def progress_key(token):
    return 'dispatch_progress:' + token


def claimed(engine, token, job):
    context = _context.get()
    if context is None:
        return
    if token != context['token']:
        raise RuntimeError('Dispatch attempt identity changed')
    context['claimed'] = True
    context['staged'].set()


def progress(phase, *, message=None):
    context = _context.get()
    if not context or not context.get('token'):
        return
    from core.db import engine
    with engine.begin() as conn:
        key = progress_key(context['token'])
        record = load(conn, key)
        if record is None:
            raise RuntimeError('Dispatch progress is missing')
        record['phase'] = phase
        if message is not None:
            record['message'] = message[:500]
        if phase in ('start_accepted', 'refused', 'needs_reconciliation'):
            final_key = result_key(record['job_id'])
            if not insert(conn, final_key, record):
                save(conn, final_key, record)
            conn.execute(text('DELETE FROM system_config WHERE key=:key'), {'key': key})
        else:
            save(conn, key, record)


def stage(printer_id, job_id, *, operation=None, on_staged=None):
    """Run authorized dispatch in a worker; handshake before any hardware I/O."""
    from core.db import engine
    with engine.connect() as conn:
        if conn.execute(text("SELECT token FROM printer_dispatch_claims WHERE (printer_id=:printer OR job_id=:job) AND (state<>'accepted' OR job_id=:job)"),
                        {'printer': printer_id, 'job': job_id}).first():
            raise HTTPException(409, 'A dispatch attempt already owns this job or printer')
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, 'Dispatch workers are busy; no attempt was staged')
    state = {'staged': Event(), 'token': uuid.uuid4().hex}
    try:
        with engine.begin() as conn:
            org_id = conn.execute(text('SELECT charged_to_org_id FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one()
            insert(conn, progress_key(state['token']), {'job_id': job_id, 'printer_id': printer_id,
                'org_id': org_id, 'attempt_id': state['token'], 'phase': 'preparing',
                'retry_blocked': True, 'observation': None})
    except Exception:
        _slots.release()
        raise HTTPException(503, 'Dispatch worker could not be staged') from None
    state['handle'] = {'success': False, 'accepted': True, 'job_id': job_id,
        'attempt_id': state['token'], 'phase': 'preparing', 'status_url': status_url(job_id, state['token'])}
    inherited = copy_context()

    def run():
        marker = _context.set(state)
        try:
            from modules.printers.dispatch import dispatch_job
            result = operation() if operation else dispatch_job(printer_id, job_id)
            if isinstance(result, dict):
                success, message = result['success'], result.get('message', '')
            else:
                success, message = result
            state['result'] = (success, message)
            if state.get('token'):
                from core.db import engine
                with engine.connect() as conn:
                    claim_state = conn.execute(text('SELECT state FROM printer_dispatch_claims WHERE token=:token'),
                                               {'token': state['token']}).scalar_one_or_none()
                phase = 'start_accepted' if success else ('needs_reconciliation' if claim_state else 'refused')
                progress(phase, message=message)
                if success and operation is None:
                    try:
                        from core.audit import log_audit
                        from sqlalchemy.orm import Session
                        with Session(engine) as db:
                            log_audit(db, 'job.dispatched', 'job', job_id, {'printer_id': printer_id})
                            db.commit()
                    except Exception:
                        from modules.printers.dispatch import log
                        log.warning('Accepted dispatch audit could not be recorded')
        except Exception:
            state['result'] = (False, 'Dispatch outcome requires reconciliation before retrying')
            try:progress('needs_reconciliation', message=state['result'][1])
            except Exception:pass
        finally:
            state['staged'].set()
            _context.reset(marker)
            _slots.release()
            with _workers_lock:
                _workers.discard(current_thread())

    try:
        if on_staged is not None:
            on_staged(state['handle'])
        worker = Thread(target=lambda: inherited.run(run), name='dispatch-once', daemon=True)
        worker.attempt_id = state['token']
        with _workers_lock:
            if _draining:
                raise RuntimeError('Dispatch is shutting down')
            _workers.add(worker)
            try:
                worker.start()
            except Exception:
                _workers.discard(worker)
                raise
    except Exception:
        _slots.release()
        with engine.begin() as conn:
            conn.execute(text('DELETE FROM system_config WHERE key=:key'), {'key': progress_key(state['token'])})
        raise HTTPException(503, 'Dispatch worker could not start') from None
    # Preparation before the durable claim performs local validation only.
    # A timeout never permits retry: the worker may still finish staging.
    if not state['staged'].wait(STAGING_SECONDS):
        return state['handle']
    if state.get('claimed'):
        return state['handle']
    raise HTTPException(400, state.get('result', (False, 'Dispatch was not staged'))[1])
