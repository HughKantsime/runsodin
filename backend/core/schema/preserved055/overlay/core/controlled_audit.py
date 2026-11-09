"""Audit metadata survives while restricted details are removed from exports."""
import copy
from core.controlled_parts import allowed, member
from sqlalchemy import text


def audit_allowed(db, actor, kind, identifier, audit_id=None):
    live = kind != 'job' or db.execute(text('SELECT 1 FROM jobs WHERE id=:id'), {'id': identifier}).first()
    if live and not allowed(db, kind, identifier, actor):
        return False
    # Durable job IDs keep late terminal observations in the same audience.
    if kind == 'job':
        audiences = db.execute(text('SELECT audience_id FROM controlled_job_tombstones WHERE job_id=:id'), {'id': identifier}).scalars()
        return all(member(db, actor, audience) for audience in audiences)
    return True


def redact_audit_rows(db, actor, rows):
    result = []
    for entry, actor_id, actor_name, actor_kind in rows:
        if entry.entity_type in ('job', 'file', 'print_file', 'model') and not audit_allowed(db, actor, entry.entity_type, entry.entity_id, entry.id):
            clone = copy.copy(entry)
            clone.__dict__ = dict(entry.__dict__)
            clone.__dict__.pop('_sa_instance_state', None)
            from sqlalchemy.orm import instrumentation
            instrumentation.manager_of_class(type(entry)).setup_instance(clone)
            clone.details = '[controlled]'
            entry = clone
        result.append((entry, actor_id, actor_name, actor_kind))
    return result
