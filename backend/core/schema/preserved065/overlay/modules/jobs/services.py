"""The jobs module's cross-module service boundary.

Other modules (monitors, dispatch, education, notifications, printers) change a
job's status and react to printer deletion only through these names; the
implementations live in modules.jobs.state_machine and modules.jobs.pinning.
Education queue reads and ordering use this same boundary so scheduling and
dispatch share the jobs-owned rank and head-of-queue policy.
"""
import logging
from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError

from core.log_redaction import redact_text
from modules.jobs.education_order import education_order, waiting_rows, ordered_waiting, head_refusal
from modules.jobs.pinning import clear_pins_for_deleted_printer
from modules.jobs.state_machine import Expr, TransitionError, transition_job, transition_result

__all__ = ["clear_job_links", "deleting_job_links", "rollback_on_integrity_error", "JobLinkConflict", "delete_org_jobs", "education_order", "waiting_rows", "ordered_waiting", "head_refusal", "Expr", "TransitionError", "clear_pins_for_deleted_printer", "transition_job", "transition_result"]


def clear_job_links(db, job_ids):
    """Apply the current FK actions to locked jobs, including on older databases."""
    ids = bindparam("ids", expanding=True)
    db.execute(text("DELETE FROM alerts WHERE job_id IN :ids").bindparams(ids), {"ids": job_ids})
    db.execute(text("UPDATE spool_usage SET job_id=NULL WHERE job_id IN :ids").bindparams(ids), {"ids": job_ids})


class JobLinkConflict(Exception):
    def __init__(self, job_ids, cause):
        self.job_ids = job_ids[:20]
        self.cause = cause

    def response(self, db, path):
        db.rollback()
        # PostgreSQL's DETAIL can contain row data. Log only the primary message.
        diagnostic = getattr(self.cause.orig, "diag", None)
        reason = getattr(diagnostic, "message_primary", None) or str(self.cause.orig).partition("\n")[0]
        logging.getLogger("odin.api").warning("Job deletion blocked: path=%s job_ids=%s: %s",
                                              path, self.job_ids, redact_text(reason))
        return JSONResponse(status_code=409, content={
            "code": "job_has_linked_records",
            "message": "This job has linked records and cannot be deleted.",
        })


@contextmanager
def deleting_job_links(db, job_ids):
    try:
        for start in range(0, len(job_ids), 500):
            clear_job_links(db, job_ids[start:start + 500])
        yield
    except IntegrityError as exc:
        raise JobLinkConflict(job_ids, exc) from exc


@contextmanager
def rollback_on_integrity_error(db):
    try:
        yield
    except IntegrityError:
        # SQLite leaves the transaction open when a deferred constraint fails at commit.
        db.rollback()
        raise


def delete_org_jobs(db, org_id):
    """Remove owned jobs and their cascading history. Return the number removed."""
    from modules.jobs.models import Job
    from core.print_attempts import clear_job_attempts, sqlalchemy_runner
    from core.controlled_parts import assert_group_deletable
    assert_group_deletable(db, org_id)
    from core.controlled_deletion import controlled_job_ids, redact_job_history_ids
    owned = db.query(Job.id).filter(Job.charged_to_org_id == org_id).order_by(Job.id).with_for_update()
    job_ids = [row.id for row in owned]
    restricted = controlled_job_ids(db, job_ids)
    for job_id in job_ids:
        from core.traceability_delivery import prepare_job_deletion
        prepare_job_deletion(db, job_id)
        clear_job_attempts(sqlalchemy_runner(db), job_id)
    with deleting_job_links(db, job_ids):
        removed = db.execute(text('DELETE FROM jobs WHERE charged_to_org_id=:org'), {'org':org_id})
    if removed.rowcount != len(job_ids):
        db.rollback()
        raise HTTPException(409, "The job list changed. Try again.")
    redact_job_history_ids(db, restricted)
    return removed.rowcount


def scheduler_material_modes():
    """Service-layer entry for other modules (settings): the allowed scheduler material modes."""
    from modules.jobs.scheduler_materials import MODES
    return MODES


def scheduler_material_mode(db):
    from modules.jobs.scheduler_materials import get_mode
    return get_mode(db)


def set_scheduler_material_mode(db, mode):
    from modules.jobs.scheduler_materials import set_mode
    return set_mode(db, mode)


def printer_matches_sliced_model(printer, required_model):
    """Service-layer entry for other modules: the scheduler's own "is this the printer model the
    file was sliced for" rule (scheduler._same_printer_model on scheduler._printer_identity)."""
    from modules.jobs.scheduler import _printer_identity, _same_printer_model
    return _same_printer_model(_printer_identity(printer), required_model)


def job_actual_costs(db, jobs, config, user):
    """Service-layer entry for other modules (reporting): {job_id: cost_actual result} for jobs
    the caller has already been allowed to see, with a fixed number of statements per 500 jobs."""
    from modules.jobs.cost_actual import cost_actual, load_attempts
    attempts = load_attempts(db, [job.id for job in jobs], user)
    return {job.id: cost_actual(attempts[job.id], config, job.quantity_on_bed) for job in jobs}


def annotate_print_failures(db, rows, *, archive=False, user=None):
    from .print_failure import annotate
    return annotate(db, rows, archive=archive, user=user)
