"""Central database policy checks for Education-owned resources."""

from __future__ import annotations

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
