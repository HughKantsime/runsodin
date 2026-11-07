"""Education lifecycle policy extracted from the public policy module."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import uuid
from contextlib import contextmanager
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError
from sqlalchemy.orm import Session
from core.interfaces.education_policy import is_education_reserved_filename, parse_education_token
from modules.jobs.services import transition_job, transition_result, Expr



def claim_monitor_observation(
    executor,
    *,
    print_job_id: int,
    printer_id: int,
    observed_filename: str | None,
    _prepared_start=None,
) -> dict:
    """Claim an exact-token observation or quarantine the reserved namespace."""
    token = parse_education_token(observed_filename)
    adapter = owner._ExecutionAdapter(executor)
    if token is None:
        linked = adapter.fetchone(
            "SELECT s.id AS submission_id,s.job_id,s.lifecycle_revision "
            "FROM print_jobs p JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if linked:
            return {
                "education_owned": True,
                "education_reserved": False,
                "authorized": False,
                "transitioned": False,
                **linked,
            }
        return {"education_owned": False, "education_reserved": False, "authorized": False}

    observation = adapter.fetchone(
        "SELECT filename,job_name FROM print_jobs WHERE id=:print_job_id "
        "AND printer_id=:printer_id AND status='running'",
        {"print_job_id": print_job_id, "printer_id": printer_id},
    )
    persisted_token = (
        parse_education_token(observation.get("filename"))
        or parse_education_token(observation.get("job_name"))
        if observation
        else None
    )
    if persisted_token != token:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": False,
        }

    digest = owner.education_token_digest(token)
    context = adapter.fetchone(
        "SELECT mc.claim_id,mc.org_id,mc.submission_id,mc.job_id,mc.printer_id,"
        "mc.authority_revision,mc.state,mc.token_digest,s.cost_center_id,s.submitted_by "
        "FROM education_monitor_claims mc JOIN education_submissions s "
        "ON s.id=mc.submission_id AND s.org_id=mc.org_id "
        "WHERE mc.token_digest=:digest AND mc.printer_id=:printer_id",
        {"digest": digest, "printer_id": printer_id},
    )
    if not context:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": False,
        }

    revision = int(context["authority_revision"])
    claim_state = context["state"]
    if claim_state == "running":
        from core.dispatch_observation_executor import TransactionView
        view=TransactionView(executor)
        # Resume revalidates the same authority order as the transition path.
        for table,where,column in (
            ('education_submissions','id=:submission_id AND org_id=:org_id','updated_at'),
            ('jobs','id=:job_id','status'),
            ('education_monitor_claims','claim_id=:claim_id','updated_at'),
            ('print_jobs','id=:print_job_id AND printer_id=:printer_id','status'),
        ):
            statement=(f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if view.dialect.name=='postgresql' else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1')
            if view.execute(text(statement),{**context,'print_job_id':print_job_id}).first() is None:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column iterate the literal locks tuple in this function (closed set); values are bound
                return {'education_owned':True,'education_reserved':True,'authorized':False,'transitioned':False}
        linked = adapter.fetchone(
            "SELECT id FROM print_jobs WHERE id=:print_job_id AND printer_id=:printer_id "
            "AND scheduled_job_id=:job_id AND status='running'",
            {
                "print_job_id": print_job_id,
                "printer_id": printer_id,
                "job_id": context["job_id"],
            },
        )
        from core.dispatch_observations import education_observation_authority
        linked=bool(linked) and education_observation_authority(view,{**context,'authority_revision':revision},print_job_id)
        result = {
            "education_owned": True,
            "education_reserved": True,
            "dispatch_receipt_binding": 'refused',
            "authorized": bool(linked),
            "transitioned": False,
            "idempotent": bool(linked),
            "submission_id": context["submission_id"],
            "job_id": context["job_id"],
            "lifecycle_revision": revision,
        }
        if linked:
            from core.dispatch_observations import bind_education_observation
            from core import auth
            result['dispatch_receipt_binding']=bind_education_observation(executor,print_job_id=print_job_id,context={**context,'authority_revision':revision},key=auth.SECRET_KEY.encode())
        return result

    link_guard = 'scheduled_job_id IS NULL'
    if _prepared_start is not None:
        from core.attempt_start_plan import _record
        from core.economic_source_creation import SourceCreationError
        scope, plan = _prepared_start
        record = _record(executor, scope, plan)
        created = record['created']
        if created is None or created.id != print_job_id or record.get('education') != context or plan.job_id != context['job_id']:
            raise SourceCreationError('Education prepared link differs')
        link_guard = '(scheduled_job_id IS NULL OR scheduled_job_id=:job_id)'

    recovered = claim_state == "reserved"
    expected_submission_status = "scheduled" if recovered else "printing"
    expected_job_status = "scheduled" if recovered else "printing"
    next_revision = revision + 1 if recovered else revision
    try:
        with adapter.savepoint():
            if recovered:
                submission_changed = adapter.execute(
                    "UPDATE education_submissions SET status='printing',"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND org_id=:org_id AND job_id=:job_id "
                    "AND status=:status AND lifecycle_revision=:revision "
                    "AND approved_printer_id=:printer_id",
                    {**context, "status": expected_submission_status, "revision": revision},
                )
            else:
                submission_changed = adapter.execute(
                    "UPDATE education_submissions SET updated_at=updated_at "
                    "WHERE id=:submission_id AND org_id=:org_id AND job_id=:job_id "
                    "AND status=:status AND lifecycle_revision=:revision "
                    "AND approved_printer_id=:printer_id",
                    {**context, "status": expected_submission_status, "revision": revision},
                )
            if submission_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            if recovered:
                job_changed = transition_result(adapter.executor, context["job_id"], "printing", "education",
                    from_states=(expected_job_status,),
                    extra_fields={"actual_start": Expr("COALESCE(actual_start,CURRENT_TIMESTAMP)")},
                    where="charged_to_org_id=:org_id AND printer_id=:printer_id",
                    params={"org_id": context["org_id"], "printer_id": context["printer_id"]})
            else:
                job_changed = adapter.execute(
                    "UPDATE jobs SET status=status WHERE id=:job_id AND charged_to_org_id=:org_id "
                    "AND status=:status AND printer_id=:printer_id",
                    {**context, "status": expected_job_status},
                )
            if job_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            claim_changed = adapter.execute(
                "UPDATE education_monitor_claims SET state='running',"
                "authority_revision=:next_revision,updated_at=CURRENT_TIMESTAMP "
                "WHERE claim_id=:claim_id AND state=:claim_state "
                "AND authority_revision=:revision AND printer_id=:printer_id",
                {
                    **context,
                    "claim_state": claim_state,
                    "revision": revision,
                    "next_revision": next_revision,
                },
            )
            if claim_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            observation_changed = adapter.execute(
                "UPDATE print_jobs SET scheduled_job_id=:job_id "
                "WHERE id=:print_job_id AND printer_id=:printer_id "
                f"AND {link_guard} AND status='running'",
                {
                    "job_id": context["job_id"],
                    "print_job_id": print_job_id,
                    "printer_id": printer_id,
                },
            )
            if observation_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            from core.print_attempts import bind_print_attempt
            bind_print_attempt(adapter.execute, print_job_id, context["job_id"])
            result = {
                "education_owned": True,
                "education_reserved": True,
                "authorized": True,
                "transitioned": True,
                "recovered": recovered,
                "submission_id": context["submission_id"],
                "job_id": context["job_id"],
                "lifecycle_revision": next_revision,
            }
            from core.dispatch_observations import bind_education_observation
            from core import auth
            result['dispatch_receipt_binding']=bind_education_observation(executor,print_job_id=print_job_id,context={**context,'authority_revision':next_revision},key=auth.SECRET_KEY.encode())
            owner._record_monitor_transition(
                adapter,
                context=context,
                action=(
                    "submission.dispatch_recovered" if recovered else "submission.print_observed"
                ),
                revision=next_revision,
                details={"claim_state": claim_state, "print_job_id": print_job_id},
                result=result,
            )
    except owner._MonitorTransitionMiss:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": False,
            "transitioned": False,
            "submission_id": context["submission_id"],
            "job_id": context["job_id"],
            "lifecycle_revision": revision,
        }
    return result



def classify_monitor_observation(
    executor,
    *,
    printer_id: int,
    print_job_id: int | None = None,
    observed_filename: str | None = None,
) -> dict:
    """Classify a monitor packet before any generic external side effects."""
    adapter = owner._ExecutionAdapter(executor)
    identities = [observed_filename]
    if print_job_id is not None:
        linked = adapter.fetchone(
            "SELECT s.id AS submission_id,s.job_id,s.lifecycle_revision,p.filename,p.job_name "
            "FROM print_jobs p LEFT JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if linked:
            identities.extend((linked.get("filename"), linked.get("job_name")))
            _, reserved, conflict = owner._resolve_monitor_tokens(*identities)
            if conflict:
                return {"education_owned": False, "education_reserved": reserved, "authorized": False}
            if linked.get("submission_id") is not None:
                return {
                    "education_owned": True,
                    "education_reserved": True,
                    "authorized": True,
                    "submission_id": linked["submission_id"],
                    "job_id": linked["job_id"],
                    "lifecycle_revision": linked["lifecycle_revision"],
                }
    token, reserved, conflict = owner._resolve_monitor_tokens(*identities)
    if token is None or conflict:
        return {"education_owned": False, "education_reserved": reserved, "authorized": False}
    claim = adapter.fetchone(
        "SELECT submission_id,job_id,authority_revision FROM education_monitor_claims "
        "WHERE token_digest=:digest AND printer_id=:printer_id",
        {"digest": owner.education_token_digest(token), "printer_id": printer_id},
    )
    if claim:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": True,
            "submission_id": claim["submission_id"],
            "job_id": claim["job_id"],
            "lifecycle_revision": claim["authority_revision"],
        }
    return {"education_owned": False, "education_reserved": True, "authorized": False}



def terminal_monitor_observation(
    executor,
    *,
    print_job_id: int,
    printer_id: int,
    terminal_status: str,
    ended_at=None,
    duration_seconds: float | None = None,
    error_code: str | None = None,
    observed_filename: str | None = None,
) -> dict:
    """Atomically apply terminal truth for an Education-owned observation."""
    if terminal_status not in {"completed", "failed", "cancelled"}:
        raise ValueError("Unsupported Education terminal status")
    sanitized_error = str(error_code)[:100] if error_code else None
    bounded_duration = (
        max(0.0, min(float(duration_seconds), 31_536_000.0))
        if duration_seconds is not None
        else None
    )
    adapter = owner._ExecutionAdapter(executor)
    context = adapter.fetchone(
        "SELECT p.id AS print_job_id,p.status AS print_status,p.scheduled_job_id,p.filename,p.job_name,"
        "s.id AS submission_id,s.org_id,s.cost_center_id,s.submitted_by,s.job_id,"
        "s.status AS submission_status,s.lifecycle_revision,s.approved_printer_id,"
        "j.status AS job_status,mc.claim_id,mc.state AS claim_state,"
        "mc.authority_revision,mc.token_digest FROM print_jobs p "
        "LEFT JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
        "LEFT JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
        "LEFT JOIN education_monitor_claims mc ON mc.job_id=s.job_id AND mc.org_id=s.org_id "
        "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
        {"print_job_id": print_job_id, "printer_id": printer_id},
    )
    if not context:
        return {"education_owned": False, "education_reserved": False, "transitioned": False}
    _, reserved_identity, conflicting_identity = owner._resolve_monitor_tokens(
        context.get("filename"), context.get("job_name"), observed_filename)
    if conflicting_identity:
        return {"education_owned": context.get("submission_id") is not None,
                "education_reserved": reserved_identity, "authorized": False,
                "transitioned": False}
    if context.get("submission_id") is None:
        token, reserved, conflict = owner._resolve_monitor_tokens(
            context.get("filename"), context.get("job_name"), observed_filename)
        if not reserved:
            return {"education_owned": False, "education_reserved": False, "transitioned": False}
        if conflict or token is None:
            return {"education_owned": False, "education_reserved": True,
                    "authorized": False, "transitioned": False}
        if observed_filename is not None and parse_education_token(observed_filename) != token:
            return {"education_owned": False, "education_reserved": True,
                    "authorized": False, "transitioned": False}
        quarantined = adapter.execute(
            "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
            "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
            "AND printer_id=:printer_id AND scheduled_job_id IS NULL AND status='running' "
            "AND COALESCE(filename,'')=COALESCE(:observed_stored_filename,'') "
            "AND COALESCE(job_name,'')=COALESCE(:observed_stored_job_name,'')",
            {
                "status": terminal_status,
                "ended_at": ended_at,
                "error_code": sanitized_error,
                "print_job_id": print_job_id,
                "printer_id": printer_id,
                "observed_stored_filename": context.get("filename"),
                "observed_stored_job_name": context.get("job_name"),
            },
        )
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": quarantined.rowcount == 1,
            "quarantined": quarantined.rowcount == 1,
        }

    base_result = {
        "education_owned": True,
        "education_reserved": True,
        "submission_id": context["submission_id"],
        "job_id": context["job_id"],
        "lifecycle_revision": context["lifecycle_revision"],
    }
    expected_digest = None
    if observed_filename is not None:
        observed_token = parse_education_token(observed_filename)
        expected_digest = owner.education_token_digest(observed_token) if observed_token else None
        if expected_digest is None or expected_digest != context.get("token_digest"):
            return {**base_result, "transitioned": False, "idempotent": False, "authorized": False}
    if (
        context["submission_status"] == terminal_status
        and context["job_status"] == terminal_status
        and context["print_status"] == terminal_status
        and context.get("claim_id") is None
    ):
        return {
            **base_result,
            "transitioned": False,
            "idempotent": True,
            "authorized": True,
        }
    def physical_end() -> bool:
        # The job's Education authority refused this end before any change
        # (operator cancel, revoked claim, stale revision...), but the printer's
        # own linked print really ended: record that physical fact only. Submission, job and claim are
        # untouched; monitors publish the physical completion without the job.
        return adapter.execute(
            "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
            "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
            "AND printer_id=:printer_id AND scheduled_job_id=:job_id AND status='running'",
            {"status": terminal_status, "ended_at": ended_at, "error_code": sanitized_error,
             "print_job_id": print_job_id, "printer_id": printer_id, "job_id": context["job_id"]},
        ).rowcount == 1

    if (
        context.get("claim_id") is None
        or context.get("claim_state") != "running"
        or context["submission_status"] != "printing"
        or context["job_status"] != "printing"
        or int(context["authority_revision"]) != int(context["lifecycle_revision"])
        or int(context["approved_printer_id"]) != int(printer_id)
    ):
        return {
            **base_result,
            "transitioned": False,
            "idempotent": False,
            "authorized": False,
            "physical_ended": physical_end(),
        }

    revision = int(context["lifecycle_revision"])
    next_revision = revision + 1
    try:
        with adapter.savepoint():
            submission_changed = adapter.execute(
                "UPDATE education_submissions SET status=:status,"
                "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:submission_id AND org_id=:org_id AND job_id=:job_id "
                "AND status='printing' AND lifecycle_revision=:revision "
                "AND approved_printer_id=:printer_id",
                {**context, "status": terminal_status, "revision": revision, "printer_id": printer_id},
            )
            if submission_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            job_changed = transition_result(adapter.executor, context["job_id"], terminal_status, "education",
                from_states=("printing",),
                extra_fields={"actual_end": Expr("COALESCE(:ended_at,CURRENT_TIMESTAMP)", ended_at=ended_at)},
                where="charged_to_org_id=:org_id AND printer_id=:printer_id",
                params={"org_id": context["org_id"], "printer_id": printer_id})
            if job_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            claim_deleted = adapter.execute(
                "DELETE FROM education_monitor_claims WHERE claim_id=:claim_id "
                "AND submission_id=:submission_id AND job_id=:job_id AND printer_id=:printer_id "
                "AND authority_revision=:revision AND state='running' "
                "AND (CAST(:expected_digest AS TEXT) IS NULL OR token_digest=:expected_digest)",
                {**context, "revision": revision, "printer_id": printer_id, "expected_digest": expected_digest},
            )
            if claim_deleted.rowcount != 1:
                raise owner._MonitorTransitionMiss
            observation_changed = adapter.execute(
                "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
                "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
                "AND printer_id=:printer_id AND scheduled_job_id=:job_id AND status='running'",
                {
                    **context,
                    "status": terminal_status,
                    "ended_at": ended_at,
                    "error_code": sanitized_error,
                    "printer_id": printer_id,
                },
            )
            if observation_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            result = {
                **base_result,
                "transitioned": True,
                "idempotent": False,
                "authorized": True,
                "lifecycle_revision": next_revision,
            }
            owner._record_monitor_transition(
                adapter,
                context=context,
                action=f"submission.print_{terminal_status}",
                revision=next_revision,
                details={
                    "from_status": "printing",
                    "to_status": terminal_status,
                    "duration_seconds": bounded_duration,
                    "error_code": sanitized_error,
                    "print_job_id": print_job_id,
                },
                result=result,
            )
    except owner._MonitorTransitionMiss:
        final = adapter.fetchone(
            "SELECT p.status AS print_status,s.status AS submission_status,"
            "s.lifecycle_revision,j.status AS job_status,mc.claim_id "
            "FROM print_jobs p JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
            "LEFT JOIN education_monitor_claims mc ON mc.job_id=s.job_id AND mc.org_id=s.org_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if (
            final
            and final["submission_status"] == terminal_status
            and final["job_status"] == terminal_status
            and final["print_status"] == terminal_status
            and final.get("claim_id") is None
        ):
            return {
                **base_result,
                "transitioned": False,
                "idempotent": True,
                "authorized": True,
                "lifecycle_revision": final["lifecycle_revision"],
            }
        # Authority changed concurrently (e.g. the claim digest was replaced):
        # identity is uncertain, so nothing -- not even the physical end -- changes.
        return {
            **base_result,
            "transitioned": False,
            "idempotent": False,
            "authorized": False,
        }
    return result

# Import after definitions so both the public facade and this module can load first.
import modules.organizations.education_policy as owner
