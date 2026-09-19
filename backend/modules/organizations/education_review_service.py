"""Private Education submission projections and atomic teacher review decisions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.errors import ErrorCode, OdinError
from modules.organizations.education_admin_support import (
    claim_command,
    complete_command,
)
from modules.organizations.education_access import (
    CURSOR_ORDER_VERSION,
    decode_cursor,
    encode_cursor,
)
from modules.printers.services import evaluate_submission_compatibility


VISIBLE_STATUSES = (
    "submitted",
    "pending",
    "scheduled",
    "printing",
    "completed",
    "failed",
    "rejected",
    "cancelled",
)


def _not_found() -> OdinError:
    return OdinError(ErrorCode.not_found, "Submission not found", status=404)


def _org_id(principal: dict) -> int:
    value = principal.get("group_id")
    if value is None:
        raise _not_found()
    return int(value)


def _tenant_admin(principal: dict) -> bool:
    return principal.get("role") == "admin" and principal.get("group_id") is not None


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _cursor_time(value) -> int:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("submission timestamp is not cursor-compatible")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000)


def _projection(row) -> dict:
    return {
        "id": int(row.id),
        "job_id": int(row.job_id),
        "cost_center_id": int(row.cost_center_id),
        "submitted_by": int(row.submitted_by),
        "submitter_username": row.submitter_username,
        "item_name": row.item_name,
        "status": row.status,
        "approved_printer_id": (
            int(row.approved_printer_id) if row.approved_printer_id is not None else None
        ),
        "lifecycle_revision": int(row.lifecycle_revision),
        "compatibility_engine_version": row.compatibility_engine_version,
        "rejection_reason": row.rejected_reason if row.status == "rejected" else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _decision_hash(submission_id: int, body) -> str:
    payload = {
        "submission_id": int(submission_id),
        "body": body.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_BASE_SELECT = (
    "SELECT s.id,s.org_id,s.job_id,s.print_file_id,s.cost_center_id,s.submitted_by,"
    "s.status,s.approved_printer_id,s.approved_by,s.lifecycle_revision,"
    "s.compatibility_engine_version,s.created_at,s.updated_at,c.state AS center_state,"
    "j.status AS job_status,j.item_name,j.rejected_reason,u.username AS submitter_username "
    "FROM education_submissions s "
    "JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id "
    "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
    "JOIN users u ON u.id=s.submitted_by AND u.group_id=s.org_id "
)


def list_visible_submissions(
    db: Session,
    *,
    principal: dict,
    status: str | None = None,
    cost_center_id: int | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    org_id = _org_id(principal)
    if status is not None and status not in VISIBLE_STATUSES:
        raise OdinError(
            ErrorCode.validation_failed,
            "Invalid submission status",
            status=422,
            extra={"fields": ["status"]},
        )
    audience = (
        "(:is_admin=1 OR s.submitted_by=:user_id OR EXISTS ("
        "SELECT 1 FROM education_cost_center_grants g WHERE g.org_id=s.org_id "
        "AND g.cost_center_id=s.cost_center_id AND g.user_id=:user_id "
        "AND g.role='manager' AND g.state='active' AND c.state='active'))"
    )
    filters = ["s.org_id=:org_id", audience]
    params = {
        "org_id": org_id,
        "user_id": int(principal["id"]),
        "is_admin": 1 if _tenant_admin(principal) else 0,
    }
    if status is not None:
        filters.append("s.status=:status")
        params["status"] = status
    if cost_center_id is not None:
        filters.append("s.cost_center_id=:center_id")
        params["center_id"] = cost_center_id
    if cursor:
        payload = decode_cursor(cursor)
        expected = {
            "kind": "submissions",
            "org_id": org_id,
            "user_id": int(principal["id"]),
            "is_admin": bool(_tenant_admin(principal)),
            "status": status,
            "cost_center_id": cost_center_id,
            "limit": limit,
            "order_version": CURSOR_ORDER_VERSION,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise OdinError(
                ErrorCode.invalid_cursor,
                "Education cursor does not match request",
                status=400,
            )
        last = payload.get("last")
        if (
            not isinstance(last, list)
            or len(last) != 2
            or not isinstance(last[0], int)
            or not isinstance(last[1], int)
        ):
            raise OdinError(ErrorCode.invalid_cursor, "Invalid Education cursor", status=400)
    else:
        last = None
    rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- SQL fragments are fixed internal clauses; every request value is bound
            _BASE_SELECT
            + " WHERE "
            + " AND ".join(filters)
            + " ORDER BY s.created_at DESC,s.id DESC"
        ),
        params,
    ).fetchall()
    try:
        filtered_rows = [
            row
            for row in rows
            if last is None or (_cursor_time(row.created_at), int(row.id)) < tuple(last)
        ]
    except (TypeError, ValueError) as exc:
        raise OdinError(
            ErrorCode.internal_error,
            "Submission queue contains an invalid timestamp",
            status=503,
            retriable=True,
        ) from exc
    page = filtered_rows[:limit]
    next_cursor = None
    if len(filtered_rows) > limit and page:
        last_row = page[-1]
        next_cursor = encode_cursor(
            {
                "kind": "submissions",
                "org_id": org_id,
                "user_id": int(principal["id"]),
                "is_admin": bool(_tenant_admin(principal)),
                "status": status,
                "cost_center_id": cost_center_id,
                "limit": limit,
                "order_version": CURSOR_ORDER_VERSION,
                "last": [_cursor_time(last_row.created_at), int(last_row.id)],
            }
        )
    return {"items": [_projection(row) for row in page], "next_cursor": next_cursor}


def _reviewable_submission(db: Session, *, submission_id: int, principal: dict):
    org_id = _org_id(principal)
    row = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- base query is a fixed module constant; every request value is bound
            _BASE_SELECT
            + " WHERE s.id=:id AND s.org_id=:org_id AND ("
            ":is_admin=1 OR EXISTS (SELECT 1 FROM education_cost_center_grants g "
            "WHERE g.org_id=s.org_id AND g.cost_center_id=s.cost_center_id "
            "AND g.user_id=:user_id AND g.role='manager' AND g.state='active'))"
        ),
        {
            "id": submission_id,
            "org_id": org_id,
            "user_id": principal["id"],
            "is_admin": 1 if _tenant_admin(principal) else 0,
        },
    ).fetchone()
    if not row:
        raise _not_found()
    return row


def _assert_review_state(row, revision: int) -> None:
    if int(row.lifecycle_revision) != int(revision):
        raise OdinError(
            ErrorCode.revision_conflict,
            "reload_required",
            status=409,
            extra={"reason": "stale_revision", "fields": ["revision"]},
        )
    if row.center_state != "active" or row.status != "submitted" or row.job_status != "submitted":
        raise OdinError(
            ErrorCode.invalid_state_transition,
            "Submission is no longer awaiting review",
            status=409,
        )


def preview_submission_compatibility(
    db: Session,
    *,
    submission_id: int,
    printer_id: int,
    revision: int,
    principal: dict,
) -> dict:
    row = _reviewable_submission(db, submission_id=submission_id, principal=principal)
    _assert_review_state(row, revision)
    entitled = db.execute(
        text(
            "SELECT 1 FROM education_cost_center_printers e JOIN printers p "
            "ON p.id=e.printer_id AND p.org_id=e.org_id "
            "WHERE e.org_id=:org_id AND e.cost_center_id=:center_id "
            "AND e.printer_id=:printer_id AND e.state='active' AND p.is_active IS TRUE "
            "AND p.shared IS NOT TRUE"
        ),
        {
            "org_id": row.org_id,
            "center_id": row.cost_center_id,
            "printer_id": printer_id,
        },
    ).fetchone()
    if not entitled:
        raise _not_found()
    compatibility = evaluate_submission_compatibility(
        db,
        org_id=int(row.org_id),
        print_file_id=int(row.print_file_id),
        printer_id=printer_id,
    )
    if not compatibility:
        raise OdinError(
            ErrorCode.internal_error,
            "Compatibility evaluation is unavailable",
            status=503,
            retriable=True,
        )
    return {
        "submission_id": int(row.id),
        "printer_id": printer_id,
        "lifecycle_revision": int(row.lifecycle_revision),
        **compatibility,
    }


def _record_decision(
    db: Session,
    *,
    row,
    principal: dict,
    action: str,
    command_id: str,
    digest: str,
    result: dict,
    details: dict,
) -> None:
    event_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO education_audit_events "
            "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,"
            "resource_type,resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
            "VALUES (:event_id,:org_id,'user',:actor_id,:action,:command_id,:digest,"
            "'submission',:resource_id,:center_id,:revision,:details,:result)"
        ),
        {
            "event_id": event_id,
            "org_id": row.org_id,
            "actor_id": str(principal["id"]),
            "action": action,
            "command_id": command_id,
            "digest": digest,
            "resource_id": str(row.id),
            "center_id": row.cost_center_id,
            "revision": result["lifecycle_revision"],
            "details": json.dumps(jsonable_encoder(details), sort_keys=True),
            "result": json.dumps(jsonable_encoder(result), sort_keys=True),
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
            "org_id": row.org_id,
            "user_id": row.submitted_by,
        },
    )
    complete_command(
        db,
        org_id=int(row.org_id),
        actor_id=int(principal["id"]),
        action=action,
        command_id=command_id,
        result=result,
    )


def _discard_pending_claim(
    db: Session,
    *,
    row,
    principal: dict,
    action: str,
    command_id: str,
    digest: str,
) -> None:
    db.execute(
        text(
            "DELETE FROM education_commands WHERE org_id=:org_id AND actor_kind='user' "
            "AND actor_id=:actor_id AND action=:action AND command_id=:command_id "
            "AND request_hash=:digest AND state='pending'"
        ),
        {
            "org_id": row.org_id,
            "actor_id": str(principal["id"]),
            "action": action,
            "command_id": command_id,
            "digest": digest,
        },
    )
    db.commit()


def approve_submission(db: Session, *, submission_id: int, body, principal: dict) -> dict:
    row = _reviewable_submission(db, submission_id=submission_id, principal=principal)
    action = "submission.approve"
    command_id = str(body.command_id)
    digest = _decision_hash(submission_id, body)
    claimed = False
    try:
        replay = claim_command(
            db,
            org_id=int(row.org_id),
            actor_id=int(principal["id"]),
            action=action,
            command_id=command_id,
            request_digest=digest,
        )
        if replay:
            return replay
        claimed = True
        _assert_review_state(row, body.revision)
        entitled = db.execute(
            text(
                "SELECT 1 FROM education_cost_center_printers e JOIN printers p "
                "ON p.id=e.printer_id AND p.org_id=e.org_id "
                "WHERE e.org_id=:org_id AND e.cost_center_id=:center_id "
                "AND e.printer_id=:printer_id AND e.state='active' AND p.is_active IS TRUE "
                "AND p.shared IS NOT TRUE"
            ),
            {
                "org_id": row.org_id,
                "center_id": row.cost_center_id,
                "printer_id": body.printer_id,
            },
        ).fetchone()
        if not entitled:
            raise OdinError(ErrorCode.not_found, "Printer not found", status=404)
        compatibility = evaluate_submission_compatibility(
            db,
            org_id=int(row.org_id),
            print_file_id=int(row.print_file_id),
            printer_id=int(body.printer_id),
        )
        if not compatibility or not compatibility["compatible"]:
            reasons = compatibility["reasons"] if compatibility else []
            raise OdinError(
                ErrorCode.resource_conflict,
                "Submission is not compatible with the selected printer",
                status=409,
                extra={"compatibility_reasons": reasons},
            )
        next_revision = int(row.lifecycle_revision) + 1
        changed = db.execute(
            text(
                "UPDATE education_submissions SET status='pending',approved_printer_id=:printer_id,"
                "approved_by=:actor_id,compatibility_engine_version=:engine,"
                "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:id AND org_id=:org_id AND status='submitted' "
                "AND lifecycle_revision=:revision AND EXISTS (SELECT 1 FROM education_cost_centers c "
                "WHERE c.id=education_submissions.cost_center_id AND c.org_id=education_submissions.org_id "
                "AND c.state='active') AND (:is_admin=1 OR EXISTS (SELECT 1 FROM "
                "education_cost_center_grants g WHERE g.org_id=education_submissions.org_id "
                "AND g.cost_center_id=education_submissions.cost_center_id AND g.user_id=:actor_id "
                "AND g.role='manager' AND g.state='active')) AND EXISTS (SELECT 1 FROM "
                "education_cost_center_printers e JOIN printers p ON p.id=e.printer_id "
                "AND p.org_id=e.org_id WHERE e.org_id=education_submissions.org_id "
                "AND e.cost_center_id=education_submissions.cost_center_id "
                "AND e.printer_id=:printer_id AND e.state='active' AND p.is_active IS TRUE "
                "AND p.shared IS NOT TRUE)"
            ),
            {
                "printer_id": body.printer_id,
                "actor_id": principal["id"],
                "engine": compatibility["engine_version"],
                "id": row.id,
                "org_id": row.org_id,
                "revision": body.revision,
                "is_admin": 1 if _tenant_admin(principal) else 0,
            },
        )
        job_changed = db.execute(
            text(
                "UPDATE jobs SET status='pending',printer_id=:printer_id,approved_by=:actor_id,"
                "approved_at=CURRENT_TIMESTAMP,rejected_reason=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:job_id AND charged_to_org_id=:org_id AND status='submitted'"
            ),
            {
                "printer_id": body.printer_id,
                "actor_id": principal["id"],
                "job_id": row.job_id,
                "org_id": row.org_id,
            },
        )
        if changed.rowcount != 1 or job_changed.rowcount != 1:
            raise OdinError(
                ErrorCode.revision_conflict, "reload_required", status=409, retriable=True
            )
        result = {
            "id": int(row.id),
            "job_id": int(row.job_id),
            "cost_center_id": int(row.cost_center_id),
            "status": "pending",
            "approved_printer_id": int(body.printer_id),
            "lifecycle_revision": next_revision,
            "compatibility_engine_version": compatibility["engine_version"],
            "compatibility": compatibility,
        }
        _record_decision(
            db,
            row=row,
            principal=principal,
            action=action,
            command_id=command_id,
            digest=digest,
            result=result,
            details={
                "from_status": "submitted",
                "to_status": "pending",
                "printer_id": int(body.printer_id),
                "compatibility": compatibility,
            },
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        if claimed:
            _discard_pending_claim(
                db,
                row=row,
                principal=principal,
                action=action,
                command_id=command_id,
                digest=digest,
            )
        raise


def reject_submission(db: Session, *, submission_id: int, body, principal: dict) -> dict:
    row = _reviewable_submission(db, submission_id=submission_id, principal=principal)
    action = "submission.reject"
    command_id = str(body.command_id)
    digest = _decision_hash(submission_id, body)
    claimed = False
    try:
        replay = claim_command(
            db,
            org_id=int(row.org_id),
            actor_id=int(principal["id"]),
            action=action,
            command_id=command_id,
            request_digest=digest,
        )
        if replay:
            return replay
        claimed = True
        _assert_review_state(row, body.revision)
        next_revision = int(row.lifecycle_revision) + 1
        changed = db.execute(
            text(
                "UPDATE education_submissions SET status='rejected',approved_printer_id=NULL,"
                "approved_by=NULL,compatibility_engine_version=NULL,"
                "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:id AND org_id=:org_id AND status='submitted' "
                "AND lifecycle_revision=:revision AND EXISTS (SELECT 1 FROM education_cost_centers c "
                "WHERE c.id=education_submissions.cost_center_id AND c.org_id=education_submissions.org_id "
                "AND c.state='active') AND (:is_admin=1 OR EXISTS (SELECT 1 FROM "
                "education_cost_center_grants g WHERE g.org_id=education_submissions.org_id "
                "AND g.cost_center_id=education_submissions.cost_center_id AND g.user_id=:actor_id "
                "AND g.role='manager' AND g.state='active'))"
            ),
            {
                "id": row.id,
                "org_id": row.org_id,
                "revision": body.revision,
                "actor_id": principal["id"],
                "is_admin": 1 if _tenant_admin(principal) else 0,
            },
        )
        job_changed = db.execute(
            text(
                "UPDATE jobs SET status='rejected',printer_id=NULL,approved_by=NULL,"
                "approved_at=NULL,rejected_reason=:reason,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:job_id AND charged_to_org_id=:org_id AND status='submitted'"
            ),
            {"reason": body.reason, "job_id": row.job_id, "org_id": row.org_id},
        )
        if changed.rowcount != 1 or job_changed.rowcount != 1:
            raise OdinError(
                ErrorCode.revision_conflict, "reload_required", status=409, retriable=True
            )
        result = {
            "id": int(row.id),
            "job_id": int(row.job_id),
            "cost_center_id": int(row.cost_center_id),
            "status": "rejected",
            "approved_printer_id": None,
            "lifecycle_revision": next_revision,
            "compatibility_engine_version": None,
        }
        _record_decision(
            db,
            row=row,
            principal=principal,
            action=action,
            command_id=command_id,
            digest=digest,
            result=result,
            details={
                "from_status": "submitted",
                "to_status": "rejected",
                "reason_recorded": True,
            },
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        if claimed:
            _discard_pending_claim(
                db,
                row=row,
                principal=principal,
                action=action,
                command_id=command_id,
                digest=digest,
            )
        raise
