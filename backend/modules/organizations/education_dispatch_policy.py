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



def _record_monitor_transition(
    executor: owner._ExecutionAdapter,
    *,
    context: dict,
    action: str,
    revision: int,
    details: dict,
    result: dict,
) -> None:
    """Write the only notification evidence produced by Education monitoring."""
    event_id = str(uuid.uuid4())
    claim_id = str(context.get("claim_id") or "unclaimed")
    command_id = f"monitor:{claim_id}:{action}"[:64]
    request_hash = hashlib.sha256(
        json.dumps(details, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    executor.execute(
        "INSERT INTO education_audit_events "
        "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,"
        "resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
        "VALUES (:event_id,:org_id,'system','monitor',:action,:command_id,:request_hash,"
        "'submission',:resource_id,:center_id,:revision,:details,:result)",
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "action": action,
            "command_id": command_id,
            "request_hash": request_hash,
            "resource_id": str(context["submission_id"]),
            "center_id": context["cost_center_id"],
            "revision": revision,
            "details": json.dumps(details, sort_keys=True),
            "result": json.dumps(result, sort_keys=True),
        },
    )
    executor.execute(
        "INSERT INTO education_notification_outbox (event_id,org_id,recipient_user_id) "
        "SELECT :event_id,:org_id,u.id FROM users u WHERE u.id=:user_id "
        "AND u.group_id=:org_id AND u.is_active IS TRUE "
        "ON CONFLICT (event_id,recipient_user_id) DO NOTHING",
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "user_id": context["submitted_by"],
        },
    )



def reserve_dispatch(
    executor,
    *,
    job_id: int,
    printer_id: int,
    expected_revision: int,
    extension: str,
) -> dict | None:
    """Reserve an opaque physical correlation token before hardware is called."""
    normalized_extension = str(extension or "").lower()
    if normalized_extension and not normalized_extension.startswith("."):
        normalized_extension = f".{normalized_extension}"
    if normalized_extension not in owner._ALLOWED_REMOTE_EXTENSIONS:
        return None

    from modules.organizations.education_training_policy import job_training_allowed
    if not job_training_allowed(executor, job_id=job_id, printer_id=printer_id, lock=True):
        return None
    from core import license_lock
    if not license_lock.printer_allowed_now(executor, printer_id):
        return None  # no reservation for a printer outside the license selection
    adapter = owner._ExecutionAdapter(executor)
    token = secrets.token_hex(16)
    claim_id = str(uuid.uuid4())
    digest = owner.education_token_digest(token)
    context: dict | None = None
    try:
        with adapter.savepoint():
            submission_locked = adapter.execute(
                "UPDATE education_submissions SET updated_at=updated_at "
                "WHERE job_id=:job_id AND status='scheduled' "
                "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id "
                "AND EXISTS (SELECT 1 FROM education_cost_center_printers e "
                "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
                "JOIN printers p ON p.id=e.printer_id AND p.org_id=e.org_id "
                "WHERE e.org_id=education_submissions.org_id "
                "AND e.cost_center_id=education_submissions.cost_center_id "
                "AND e.printer_id=:printer_id AND e.state='active' AND c.state='active' "
                "AND p.is_active IS TRUE AND p.shared IS NOT TRUE)",
                {
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if submission_locked.rowcount != 1:
                raise owner._MonitorTransitionMiss
            context = adapter.fetchone(
                "SELECT id AS submission_id,org_id,cost_center_id,submitted_by,job_id,"
                "lifecycle_revision,approved_printer_id FROM education_submissions "
                "WHERE job_id=:job_id",
                {"job_id": job_id},
            )
            if not context:
                raise owner._MonitorTransitionMiss
            job_locked = adapter.execute(
                "UPDATE jobs SET status=status WHERE id=:job_id AND charged_to_org_id=:org_id "
                "AND status='scheduled' AND printer_id=:printer_id",
                {
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "printer_id": printer_id,
                },
            )
            if job_locked.rowcount != 1:
                raise owner._MonitorTransitionMiss
            adapter.execute(
                "INSERT INTO education_monitor_claims "
                "(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) "
                "VALUES (:claim_id,:org_id,:submission_id,:job_id,:printer_id,:revision,:digest,'reserved')",
                {
                    "claim_id": claim_id,
                    "org_id": context["org_id"],
                    "submission_id": context["submission_id"],
                    "job_id": job_id,
                    "printer_id": printer_id,
                    "revision": expected_revision,
                    "digest": digest,
                },
            )
    except owner._MonitorTransitionMiss:
        return None
    except Exception as exc:
        if owner._is_integrity_error(exc):
            return None
        raise

    assert context is not None
    return {
        **context,
        "education_owned": True,
        "authorized": True,
        "claim_id": claim_id,
        "token": token,
        "remote_filename": f"odin-{token}{normalized_extension}",
        "printer_id": printer_id,
        "authority_revision": expected_revision,
    }



def lock_dispatch_reservation(executor, *, reservation: dict) -> bool:
    """Lock center then submission/job/claim and validate the exact reservation.

    Caller owns commit/rollback. NOWAIT avoids waiting across a conflicting
    resource order on PostgreSQL; SQLite takes its bounded engine write lock.
    """
    if not isinstance(executor, (Session, Connection)) or not isinstance(reservation, dict):
        return False
    fields=('submission_id','job_id','printer_id','authority_revision','cost_center_id','org_id')
    if any(type(reservation.get(name)) is not int or reservation[name]<=0 for name in fields):
        return False
    token=reservation.get('token')
    claim_id=reservation.get('claim_id')
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token) or not isinstance(claim_id,str):
        return False
    postgres=executor.get_bind().dialect.name=='postgresql' if isinstance(executor,Session) else executor.dialect.name=='postgresql'
    locks=(
        ('groups','id=:org_id','id'),
        ('education_cost_centers','id=:cost_center_id AND org_id=:org_id','revision'),
        ('education_cost_center_printers','cost_center_id=:cost_center_id AND printer_id=:printer_id AND org_id=:org_id','state'),
        ('education_submissions','id=:submission_id AND job_id=:job_id AND org_id=:org_id','updated_at'),
        ('jobs','id=:job_id','status'),
        ('education_monitor_claims','claim_id=:claim_id','updated_at'),
    )
    # Identifiers below are internal constants, never request text.
    for table,where,column in locks:
        sql=f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if postgres else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1'
        if executor.execute(text(sql),reservation).first() is None:return False  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column iterate the literal locks tuple in this function (closed set); values are bound
    context=owner.authorize_dispatch(executor,job_id=reservation['job_id'],printer_id=reservation['printer_id'],expected_revision=reservation['authority_revision'])
    if not context or not context.get('authorized') or context['id']!=reservation['submission_id'] or context['org_id']!=reservation['org_id'] or context['cost_center_id']!=reservation['cost_center_id']:
        return False
    matching=executor.execute(text('SELECT 1 FROM education_monitor_claims WHERE claim_id=:claim_id '
        "AND state='reserved' AND submission_id=:submission_id AND job_id=:job_id AND printer_id=:printer_id "
        'AND org_id=:org_id AND authority_revision=:authority_revision AND token_digest=:digest'),
        {**reservation,'digest':owner.education_token_digest(token)}).first()
    return matching is not None



def cancel_dispatch_reservation(
    executor,
    *,
    claim_id: str,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> bool:
    """Cancel only the still-current reservation created by this dispatch attempt."""
    adapter = owner._ExecutionAdapter(executor)
    try:
        with adapter.savepoint():
            submission_locked = adapter.execute(
                "UPDATE education_submissions SET updated_at=updated_at "
                "WHERE id=:submission_id AND job_id=:job_id AND status='scheduled' "
                "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id",
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if submission_locked.rowcount != 1:
                raise owner._MonitorTransitionMiss
            job_locked = adapter.execute(
                "UPDATE jobs SET status=status WHERE id=:job_id AND status='scheduled' "
                "AND printer_id=:printer_id",
                {"job_id": job_id, "printer_id": printer_id},
            )
            if job_locked.rowcount != 1:
                raise owner._MonitorTransitionMiss
            deleted = adapter.execute(
                "DELETE FROM education_monitor_claims WHERE claim_id=:claim_id "
                "AND submission_id=:submission_id AND job_id=:job_id "
                "AND printer_id=:printer_id AND authority_revision=:revision "
                "AND state='reserved'",
                {
                    "claim_id": claim_id,
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "printer_id": printer_id,
                    "revision": expected_revision,
                },
            )
            if deleted.rowcount != 1:
                raise owner._MonitorTransitionMiss
    except owner._MonitorTransitionMiss:
        return False
    return True



def confirm_dispatch_started(
    executor,
    *,
    claim_id: str,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> dict:
    """Atomically confirm scheduled authority after the hardware accepted the print."""
    adapter = owner._ExecutionAdapter(executor)
    context = adapter.fetchone(
        "SELECT s.id AS submission_id,s.org_id,s.cost_center_id,s.submitted_by,s.job_id,"
        "mc.claim_id FROM education_submissions s JOIN education_monitor_claims mc "
        "ON mc.submission_id=s.id AND mc.org_id=s.org_id WHERE mc.claim_id=:claim_id",
        {"claim_id": claim_id},
    )
    if not context or int(context["submission_id"]) != int(submission_id):
        return {"education_owned": True, "authorized": False, "transitioned": False}
    revision = expected_revision + 1
    try:
        with adapter.savepoint():
            submission_changed = adapter.execute(
                "UPDATE education_submissions SET status='printing',"
                "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:submission_id AND job_id=:job_id AND org_id=:org_id "
                "AND status='scheduled' AND lifecycle_revision=:revision "
                "AND approved_printer_id=:printer_id",
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if submission_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            job_changed = transition_result(adapter.executor, job_id, "printing", "education",
                from_states=("scheduled",),
                extra_fields={"actual_start": Expr("COALESCE(actual_start,CURRENT_TIMESTAMP)")},
                where="charged_to_org_id=:org_id AND printer_id=:printer_id",
                params={"org_id": context["org_id"], "printer_id": printer_id})
            if job_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            claim_changed = adapter.execute(
                "UPDATE education_monitor_claims SET state='awaiting',authority_revision=:next_revision,"
                "updated_at=CURRENT_TIMESTAMP WHERE claim_id=:claim_id AND submission_id=:submission_id "
                "AND job_id=:job_id AND printer_id=:printer_id AND authority_revision=:revision "
                "AND state='reserved'",
                {
                    "claim_id": claim_id,
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "printer_id": printer_id,
                    "revision": expected_revision,
                    "next_revision": revision,
                },
            )
            if claim_changed.rowcount != 1:
                raise owner._MonitorTransitionMiss
            result = {
                "education_owned": True,
                "authorized": True,
                "transitioned": True,
                "submission_id": submission_id,
                "job_id": job_id,
                "lifecycle_revision": revision,
            }
            owner._record_monitor_transition(
                adapter,
                context=context,
                action="submission.dispatch_started",
                revision=revision,
                details={"from_status": "scheduled", "to_status": "printing"},
                result=result,
            )
    except owner._MonitorTransitionMiss:
        return {
            "education_owned": True,
            "authorized": False,
            "transitioned": False,
            "submission_id": submission_id,
            "job_id": job_id,
        }
    return result



def start_identity_matches(context, *identities):
    """Use the existing canonical multi-identity conflict resolver."""
    token, _, conflict = owner._resolve_monitor_tokens(*identities)
    return (not conflict and token is not None and isinstance(context, dict)
            and owner.education_token_digest(token) == context.get('token_digest'))



def discover_start_authority(executor, *, printer_id, observed_filename):
    """Read canonical token candidate only; not authority without later locks."""
    token = parse_education_token(observed_filename)
    if token is None:
        return None
    return owner._ExecutionAdapter(executor).fetchone(
        "SELECT mc.claim_id,mc.org_id,mc.submission_id,mc.job_id,mc.printer_id,"
        "mc.authority_revision,mc.state,mc.token_digest,s.cost_center_id,s.submitted_by "
        "FROM education_monitor_claims mc JOIN education_submissions s "
        "ON s.id=mc.submission_id AND s.org_id=mc.org_id "
        "WHERE mc.token_digest=:digest AND mc.printer_id=:printer_id",
        {'digest': owner.education_token_digest(token), 'printer_id': printer_id})



def recheck_start_authority(executor, context):
    """Exact reserved/awaiting/running predicates; no observation row required."""
    if not isinstance(context, dict):
        return False
    fields = ('org_id','submission_id','job_id','printer_id','authority_revision','cost_center_id')
    if any(type(context.get(name)) is not int or context[name] <= 0 for name in fields):
        return False
    state = context.get('state')
    if state not in ('reserved','awaiting','running'):
        return False
    expected = 'scheduled' if state == 'reserved' else 'printing'
    return owner._ExecutionAdapter(executor).fetchone(
        "SELECT 1 FROM education_monitor_claims mc JOIN education_submissions s "
        "ON s.id=mc.submission_id AND s.org_id=mc.org_id JOIN jobs j ON j.id=mc.job_id "
        "AND j.charged_to_org_id=mc.org_id JOIN education_cost_centers c "
        "ON c.id=s.cost_center_id AND c.org_id=s.org_id "
        "JOIN education_cost_center_printers g ON g.org_id=c.org_id AND g.cost_center_id=c.id "
        "AND g.printer_id=mc.printer_id JOIN printers p ON p.id=mc.printer_id AND p.org_id=mc.org_id "
        "WHERE mc.claim_id=:claim_id AND mc.org_id=:org_id AND mc.submission_id=:submission_id "
        "AND mc.job_id=:job_id AND mc.printer_id=:printer_id AND mc.authority_revision=:authority_revision "
        "AND mc.token_digest=:token_digest AND mc.state=:state AND s.job_id=mc.job_id "
        "AND s.lifecycle_revision=mc.authority_revision AND s.status=:expected "
        "AND s.cost_center_id=:cost_center_id AND s.submitted_by=:submitted_by "
        "AND s.approved_printer_id=mc.printer_id AND j.printer_id=mc.printer_id "
        "AND CAST(j.status AS TEXT)=:expected AND c.state='active' AND g.state='active' "
        "AND p.is_active IS TRUE AND p.shared IS NOT TRUE", {**context,'expected':expected}) is not None



def lock_start_authority(executor, context):
    """Parent-first caller then center/grant/submission/job/claim acquisition."""
    if not isinstance(context,dict):
        return False
    fields = ('org_id','submission_id','job_id','printer_id','authority_revision','cost_center_id')
    if any(type(context.get(name)) is not int or context[name]<=0 for name in fields):
        return False
    from core.dispatch_observation_executor import TransactionView
    view=TransactionView(executor)
    for table,where,column in (
        ('education_cost_centers','id=:cost_center_id AND org_id=:org_id','revision'),
        ('education_cost_center_printers','cost_center_id=:cost_center_id AND printer_id=:printer_id AND org_id=:org_id','state'),
        ('education_submissions','id=:submission_id AND job_id=:job_id AND org_id=:org_id','updated_at'),
        ('jobs','id=:job_id','status'),
        ('education_monitor_claims','claim_id=:claim_id','updated_at'),
    ):
        statement=(f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if view.dialect.name=='postgresql' else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1')
        if view.execute(text(statement),context).first() is None:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column iterate the literal locks tuple in this function (closed set); values are bound
            return False
    return owner.recheck_start_authority(executor,context)



def finalize_start_authority(executor, *, scope, plan, print_job_id, context):
    """Only plan-owned fresh row or exact retained resume may widen link CAS."""
    from core.attempt_start_plan import _record
    from core.economic_source_creation import SourceCreationError
    record=_record(executor,scope,plan)
    created=record['created']
    expected_id=plan.resume_id if plan.branch in ('resume','legacy_resume') else (created.id if created else None)
    if expected_id!=print_job_id or record.get('education')!=context or plan.job_id!=context.get('job_id'):
        raise SourceCreationError('Education prepared capability differs')
    return owner.claim_monitor_observation(executor,print_job_id=print_job_id,
        printer_id=context['printer_id'],observed_filename=(record['observed']['job_name'] if record['protocol']=='bambu' else record['observed']['filename']),
        _prepared_start=(scope,plan))

# Import after definitions so both the public facade and this module can load first.
import modules.organizations.education_policy as owner
