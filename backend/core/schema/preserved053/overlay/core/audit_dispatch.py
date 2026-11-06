"""Explain automatic dispatch without attributing it to the submitting user."""
from contextlib import contextmanager
from contextvars import ContextVar
from sqlalchemy import text
from core.audit_actor import _BINDING


_caller_records = ContextVar('dispatch_caller_records_acceptance', default=False)


@contextmanager
def caller_records_acceptance():
    """Legacy synchronous callers already write their acceptance in their session."""
    marker = _caller_records.set(True)
    try:
        yield
    finally:
        _caller_records.reset(marker)


def caller_owns_acceptance():
    return _caller_records.get()


def dispatch_details(db, job_id, details):
    binding = db.info.get(_BINDING)
    if binding and binding[0].live and db in binding[0].sessions and binding[1] is not None:
        return details
    job = db.execute(text('SELECT submitted_by,approved_by FROM jobs WHERE id=:id'),
                     {'id': job_id}).mappings().one_or_none()
    result = {**(details or {}), 'trigger': 'scheduler'}
    if job:
        for source, target in (('submitted_by', 'queued_by_user_id'), ('approved_by', 'approved_by_user_id')):
            if job[source] is not None:
                result[target] = job[source]
    return result
