"""Central database policy checks for Education-owned resources."""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session


def _exists(db: Session, statement: str, params: dict) -> bool:
    return db.execute(text(statement), params).fetchone() is not None


def assert_user_tenant_change_allowed(db: Session, user_id: int) -> None:
    if _exists(
        db,
        "SELECT 1 FROM education_cost_center_grants "
        "WHERE user_id=:id OR granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers "
        "WHERE granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_centers WHERE created_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_upload_operations WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_submissions WHERE submitted_by=:id OR approved_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_notification_outbox WHERE recipient_user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_rate_counters WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_storage_accounts WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_commands WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ) or _exists(
        db,
        "SELECT 1 FROM education_audit_events WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ):
        raise HTTPException(
            status_code=409,
            detail="User tenant cannot change while Education grants or history exist",
        )


def assert_user_hard_delete_allowed(db: Session, user_id: int) -> None:
    if _exists(
        db,
        "SELECT 1 FROM education_cost_center_grants WHERE user_id=:id OR granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers "
        "WHERE granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_centers WHERE created_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_upload_operations WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_submissions WHERE submitted_by=:id OR approved_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_notification_outbox WHERE recipient_user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_rate_counters WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_storage_accounts WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_commands WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ) or _exists(
        db,
        "SELECT 1 FROM education_audit_events WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ):
        raise HTTPException(
            status_code=409,
            detail="User has Education history; use the GDPR erasure workflow",
        )


def assert_org_hard_delete_allowed(db: Session, org_id: int) -> None:
    tables = (
        "education_cost_centers",
        "education_cost_center_grants",
        "education_cost_center_printers",
        "education_commands",
        "education_upload_operations",
        "education_submissions",
        "education_audit_events",
        "education_notification_outbox",
        "education_rate_counters",
        "education_storage_accounts",
    )
    for table_name in tables:
        if _exists(
            db,
            f"SELECT 1 FROM {table_name} WHERE org_id=:id LIMIT 1",  # nosec B608 -- immutable allowlist
            {"id": org_id},
        ):
            raise HTTPException(
                status_code=409,
                detail="Organization has Education history and cannot be hard deleted",
            )


def assert_printer_tenant_change_or_delete_allowed(db: Session, printer_id: int) -> None:
    if _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers WHERE printer_id=:id LIMIT 1",
        {"id": printer_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_submissions WHERE approved_printer_id=:id LIMIT 1",
        {"id": printer_id},
    ):
        raise HTTPException(
            status_code=409,
            detail="Printer has Education entitlement or submission history",
        )


def printer_is_currently_entitled(
    db: Session, *, org_id: int, cost_center_id: int, printer_id: int
) -> bool:
    return _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers e "
        "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
        "JOIN printers p ON p.id=e.printer_id AND p.org_id=e.org_id "
        "WHERE e.org_id=:org_id AND e.cost_center_id=:center_id "
        "AND e.printer_id=:printer_id AND e.state='active' AND c.state='active' "
        "AND p.is_active IS TRUE AND (p.shared IS NULL OR p.shared IS FALSE)",
        {
            "org_id": org_id,
            "center_id": cost_center_id,
            "printer_id": printer_id,
        },
    )


def scheduler_context(db: Session, *, job_id: int) -> dict | None:
    """Resolve current scheduling authority for an Education-owned job."""
    row = db.execute(
        text(
            "SELECT s.id,s.org_id,s.cost_center_id,s.print_file_id,s.submitted_by,s.job_id,s.status,"
            "s.lifecycle_revision,s.approved_printer_id,j.status AS job_status,"
            "j.printer_id AS job_printer_id FROM education_submissions s "
            "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
            "WHERE s.job_id=:job_id"
        ),
        {"job_id": job_id},
    ).fetchone()
    if not row:
        return None
    context = dict(row._mapping)
    matching_state = (
        context["status"] == "pending" and context["job_status"] == "pending"
    ) or (
        context["status"] == "scheduled" and context["job_status"] == "scheduled"
    )
    printer_id = context["approved_printer_id"]
    context["authorized"] = bool(
        matching_state
        and printer_id is not None
        and context["job_printer_id"] == printer_id
        and printer_is_currently_entitled(
            db,
            org_id=int(context["org_id"]),
            cost_center_id=int(context["cost_center_id"]),
            printer_id=int(printer_id),
        )
    )
    return context


def _record_scheduler_transition(
    db: Session,
    *,
    context: dict,
    action: str,
    from_status: str,
    to_status: str,
    revision: int,
    reason: str | None = None,
) -> None:
    details = {"from_status": from_status, "to_status": to_status}
    if reason:
        details["reason"] = str(reason)[:160]
    result = {
        "id": int(context["id"]),
        "job_id": int(context["job_id"]),
        "status": to_status,
        "approved_printer_id": (
            int(context["approved_printer_id"])
            if to_status != "submitted" and context["approved_printer_id"] is not None
            else None
        ),
        "lifecycle_revision": revision,
    }
    command_id = f"scheduler:{context['job_id']}:{context['lifecycle_revision']}:{to_status}"
    request_hash = hashlib.sha256(
        json.dumps(details, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    event_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO education_audit_events "
            "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,"
            "resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
            "VALUES (:event_id,:org_id,'system','scheduler',:action,:command_id,:request_hash,"
            "'submission',:resource_id,:center_id,:revision,:details,:result)"
        ),
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "action": action,
            "command_id": command_id,
            "request_hash": request_hash,
            "resource_id": str(context["id"]),
            "center_id": context["cost_center_id"],
            "revision": revision,
            "details": json.dumps(details, sort_keys=True),
            "result": json.dumps(result, sort_keys=True),
        },
    )
    db.execute(
        text(
            "INSERT INTO education_notification_outbox (event_id,org_id,recipient_user_id) "
            "SELECT :event_id,:org_id,u.id FROM users u WHERE u.id=:user_id "
            "AND u.group_id=:org_id AND u.is_active IS TRUE "
            "ON CONFLICT (event_id,recipient_user_id) DO NOTHING"
        ),
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "user_id": context["submitted_by"],
        },
    )


class _ScheduleTransitionMiss(RuntimeError):
    """Internal control flow used to roll back only a scheduler savepoint."""


def advance_schedule(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> bool:
    """CAS pending to scheduled and record it without owning the transaction."""
    context = scheduler_context(db, job_id=job_id)
    if (
        not context
        or int(context["id"]) != submission_id
        or not context["authorized"]
        or context["status"] != "pending"
    ):
        return False
    try:
        with db.begin_nested():
            job_changed = db.execute(
                text(
                    "UPDATE jobs SET status='scheduled' WHERE id=:job_id "
                    "AND charged_to_org_id=:org_id AND status='pending' "
                    "AND printer_id=:printer_id"
                ),
                {
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "printer_id": printer_id,
                },
            )
            if job_changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            changed = db.execute(
                text(
                    "UPDATE education_submissions SET status='scheduled',"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND job_id=:job_id AND status='pending' "
                    "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id "
                    "AND EXISTS (SELECT 1 FROM education_cost_center_printers e "
                    "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
                    "JOIN printers p ON p.id=e.printer_id AND p.org_id=e.org_id "
                    "WHERE e.org_id=education_submissions.org_id "
                    "AND e.cost_center_id=education_submissions.cost_center_id "
                    "AND e.printer_id=:printer_id AND e.state='active' AND c.state='active' "
                    "AND p.is_active IS TRUE AND p.shared IS NOT TRUE)"
                ),
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            context["job_id"] = job_id
            _record_scheduler_transition(
                db,
                context=context,
                action="submission.schedule",
                from_status="pending",
                to_status="scheduled",
                revision=expected_revision + 1,
            )
    except _ScheduleTransitionMiss:
        return False
    return True


def reset_stale_schedule(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> bool:
    """CAS a still-authorized stale schedule back to pending without committing."""
    context = scheduler_context(db, job_id=job_id)
    if not context or int(context["id"]) != submission_id or not context["authorized"]:
        return False
    try:
        with db.begin_nested():
            changed = db.execute(
                text(
                    "UPDATE education_submissions SET status='pending',"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND job_id=:job_id AND status='scheduled' "
                    "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id"
                ),
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            job_changed = db.execute(
                text(
                    "UPDATE jobs SET status='pending',printer_id=:printer_id,scheduled_start=NULL,"
                    "scheduled_end=NULL,match_score=NULL WHERE id=:job_id AND status='scheduled' "
                    "AND printer_id=:printer_id"
                ),
                {"job_id": job_id, "printer_id": printer_id},
            )
            if job_changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            context["job_id"] = job_id
            _record_scheduler_transition(
                db,
                context=context,
                action="submission.schedule_reset",
                from_status="scheduled",
                to_status="pending",
                revision=expected_revision + 1,
            )
    except _ScheduleTransitionMiss:
        return False
    return True


def reconcile_schedule_denial(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    expected_revision: int,
    reason: str,
) -> bool:
    """Return policy/compatibility drift to submitted without committing."""
    context = scheduler_context(db, job_id=job_id)
    if not context or int(context["id"]) != submission_id:
        return False
    sanitized = str(reason or "scheduler_policy_denied")[:160]
    try:
        with db.begin_nested():
            changed = db.execute(
                text(
                    "UPDATE education_submissions SET status='submitted',approved_printer_id=NULL,"
                    "approved_by=NULL,compatibility_engine_version=NULL,"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND job_id=:job_id AND lifecycle_revision=:revision "
                    "AND status IN ('pending','scheduled')"
                ),
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                },
            )
            if changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            job_changed = db.execute(
                text(
                    "UPDATE jobs SET status='submitted',printer_id=NULL,scheduled_start=NULL,"
                    "scheduled_end=NULL,match_score=NULL,notes=CASE WHEN notes IS NULL OR notes='' "
                    "THEN :reason ELSE notes || '\n' || :reason END WHERE id=:job_id "
                    "AND status IN ('pending','scheduled')"
                ),
                {"job_id": job_id, "reason": f"Education scheduler reset: {sanitized}"},
            )
            if job_changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            context["job_id"] = job_id
            _record_scheduler_transition(
                db,
                context=context,
                action="submission.schedule_reconcile",
                from_status=context["status"],
                to_status="submitted",
                revision=expected_revision + 1,
                reason=sanitized,
            )
    except _ScheduleTransitionMiss:
        return False
    return True


def authorize_dispatch(
    db: Session, *, job_id: int, printer_id: int, expected_revision: int
) -> dict | None:
    """Re-resolve the current Education lifecycle immediately before dispatch."""
    row = db.execute(
        text(
            "SELECT s.id, s.org_id, s.cost_center_id, s.status, "
            "s.lifecycle_revision, s.approved_printer_id, j.status AS job_status, "
            "j.printer_id AS job_printer_id "
            "FROM education_submissions s "
            "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
            "WHERE s.job_id=:job_id"
        ),
        {"job_id": job_id},
    ).fetchone()
    if not row:
        return None
    context = dict(row._mapping)
    context["authorized"] = bool(
        int(context["lifecycle_revision"]) == int(expected_revision)
        and context["status"] == "scheduled"
        and context["job_status"] == "scheduled"
        and context["approved_printer_id"] == printer_id
        and context["job_printer_id"] == printer_id
        and printer_is_currently_entitled(
            db,
            org_id=int(context["org_id"]),
            cost_center_id=int(context["cost_center_id"]),
            printer_id=printer_id,
        )
    )
    return context


def reconcile_dispatch_denial(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    expected_revision: int,
    reason: str,
) -> bool:
    """CAS a drifted approval back to submitted without silently reassigning it."""
    sanitized_reason = str(reason or "dispatch_policy_denied")[:160]
    result = db.execute(
        text(
            "UPDATE education_submissions SET status='submitted', "
            "approved_printer_id=NULL, approved_by=NULL, "
            "compatibility_engine_version=NULL, "
            "lifecycle_revision=lifecycle_revision+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=:submission_id AND job_id=:job_id "
            "AND lifecycle_revision=:revision AND status IN ('pending','scheduled')"
        ),
        {
            "submission_id": submission_id,
            "job_id": job_id,
            "revision": expected_revision,
        },
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.execute(
        text(
            "UPDATE jobs SET status='submitted', printer_id=NULL, "
            "notes=CASE WHEN notes IS NULL OR notes='' THEN :reason "
            "ELSE notes || '\n' || :reason END WHERE id=:job_id"
        ),
        {"job_id": job_id, "reason": f"Education dispatch reset: {sanitized_reason}"},
    )
    db.commit()
    return True
