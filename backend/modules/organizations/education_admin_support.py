"""Shared transactional helpers for Education administration routes."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from fastapi.encoders import jsonable_encoder

from core.errors import ErrorCode, OdinError
from modules.organizations.education_access import encode_cursor


def _json(value) -> str:
    return json.dumps(
        jsonable_encoder(value), sort_keys=True, separators=(",", ":")
    )


def request_hash(body) -> str:
    payload = body.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def claim_command(
    db: Session,
    *,
    org_id: int,
    actor_id: int,
    action: str,
    command_id: str,
    request_digest: str,
) -> dict | None:
    """Claim a command inside the caller's transaction or return its snapshot.

    The unique command identity serializes concurrent requests on both SQLite
    and PostgreSQL. The savepoint keeps a uniqueness race from aborting the
    surrounding PostgreSQL transaction.
    """
    params = {
        "org_id": org_id,
        "actor_id": str(actor_id),
        "action": action,
        "command_id": command_id,
        "request_hash": request_digest,
    }
    try:
        with db.begin_nested():
            db.execute(
                text(
                    "INSERT INTO education_commands "
                    "(org_id, actor_kind, actor_id, action, command_id, request_hash, state) "
                    "VALUES (:org_id, 'user', :actor_id, :action, :command_id, "
                    ":request_hash, 'pending')"
                ),
                params,
            )
        return None
    except IntegrityError:
        # A concurrent winner may have committed its unique claim but not yet
        # published the canonical result. End our read transaction between
        # attempts so SQLite and PostgreSQL can both observe the latest state.
        row = None
        for attempt in range(50):
            db.rollback()
            row = db.execute(
                text(
                    "SELECT request_hash, state, result_json FROM education_commands "
                    "WHERE org_id=:org_id AND actor_kind='user' AND actor_id=:actor_id "
                    "AND action=:action AND command_id=:command_id"
                ),
                params,
            ).fetchone()
            if not row or row.state != "pending" or row.result_json:
                break
            if attempt < 49:
                time.sleep(0.01)
        if not row or row.request_hash != request_digest:
            raise OdinError(
                ErrorCode.idempotency_conflict,
                "command_id was reused with different input",
                status=409,
                extra={"fields": ["command_id"]},
            )
        if row.state != "complete" or not row.result_json:
            raise OdinError(
                ErrorCode.idempotency_conflict,
                "command is already in progress",
                status=409,
                retriable=True,
                extra={"fields": ["command_id"]},
            )
        return json.loads(row.result_json)


def complete_command(
    db: Session,
    *,
    org_id: int,
    actor_id: int,
    action: str,
    command_id: str,
    result: dict,
) -> None:
    changed = db.execute(
        text(
            "UPDATE education_commands SET state='complete', result_json=:result, "
            "completed_at=CURRENT_TIMESTAMP WHERE org_id=:org_id AND actor_kind='user' "
            "AND actor_id=:actor_id AND action=:action AND command_id=:command_id "
            "AND state='pending'"
        ),
        {
            "org_id": org_id,
            "actor_id": str(actor_id),
            "action": action,
            "command_id": command_id,
            "result": _json(result),
        },
    )
    if changed.rowcount != 1:
        raise RuntimeError("Education command claim was lost before completion")


def write_audit(
    db: Session,
    *,
    org_id: int,
    actor_id: int,
    action: str,
    command_id: str,
    request_digest: str,
    resource_type: str,
    resource_id: int,
    cost_center_id: int | None,
    lifecycle_revision: int | None,
    details: dict,
    result: dict,
) -> None:
    db.execute(
        text(
            "INSERT INTO education_audit_events "
            "(event_id, org_id, actor_kind, actor_id, action, command_id, request_hash, "
            "resource_type, resource_id, cost_center_id, lifecycle_revision, details_json, result_json) "
            "VALUES (:event_id, :org_id, 'user', :actor_id, :action, :command_id, :request_hash, "
            ":resource_type, :resource_id, :cost_center_id, :revision, :details, :result)"
        ),
        {
            "event_id": str(uuid.uuid4()),
            "org_id": org_id,
            "actor_id": str(actor_id),
            "action": action,
            "command_id": command_id,
            "request_hash": request_digest,
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "cost_center_id": cost_center_id,
            "revision": lifecycle_revision,
            "details": _json(details),
            "result": _json(result),
        },
    )


def mutation_context(db: Session, body, principal: dict, org_id: int, action: str):
    digest = request_hash(body)
    command_id = str(body.command_id)
    replay = claim_command(
        db,
        org_id=org_id,
        actor_id=principal["id"],
        action=action,
        command_id=command_id,
        request_digest=digest,
    )
    return command_id, digest, replay


def finish_mutation(
    db: Session,
    *,
    principal: dict,
    org_id: int,
    action: str,
    command_id: str,
    request_digest: str,
    resource_id: int,
    revision: int,
    details: dict,
    result: dict,
) -> None:
    write_audit(
        db,
        org_id=org_id,
        actor_id=principal["id"],
        action=action,
        command_id=command_id,
        request_digest=request_digest,
        resource_type="cost_center",
        resource_id=resource_id,
        cost_center_id=resource_id,
        lifecycle_revision=revision,
        details=details,
        result=result,
    )
    complete_command(
        db,
        org_id=org_id,
        actor_id=principal["id"],
        action=action,
        command_id=command_id,
        result=result,
    )


def page_sorted(
    rows: list,
    *,
    last: list | None,
    limit: int,
    key: Callable,
    cursor_payload: dict,
) -> tuple[list, str | None]:
    ordered = sorted(rows, key=key)
    filtered = [row for row in ordered if last is None or list(key(row)) > last]
    page = filtered[:limit]
    next_cursor = None
    if len(filtered) > limit and page:
        next_cursor = encode_cursor({**cursor_payload, "last": list(key(page[-1]))})
    return page, next_cursor


def center_projection(db: Session, center) -> dict:
    counts = db.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM education_cost_center_grants g "
            " WHERE g.cost_center_id=:id AND g.state='active') AS active_grants, "
            "(SELECT COUNT(*) FROM education_cost_center_printers p "
            " WHERE p.cost_center_id=:id AND p.state='active') AS active_printers, "
            "(SELECT COUNT(*) FROM education_submissions s "
            " WHERE s.cost_center_id=:id) AS submissions"
        ),
        {"id": center.id},
    ).fetchone()
    return {
        "id": int(center.id),
        "org_id": int(center.org_id),
        "name": center.display_name,
        "code": center.code,
        "description": center.description or "",
        "active": center.state == "active",
        "revision": int(center.revision),
        "created_at": center.created_at.isoformat()
        if hasattr(center.created_at, "isoformat")
        else str(center.created_at),
        "updated_at": center.updated_at.isoformat()
        if hasattr(center.updated_at, "isoformat")
        else str(center.updated_at),
        "counts": {
            "active_grants": int(counts.active_grants),
            "active_printers": int(counts.active_printers),
            "submissions": int(counts.submissions),
        },
    }


def reload_required() -> OdinError:
    return OdinError(
        ErrorCode.revision_conflict,
        "reload_required",
        status=409,
        extra={"reason": "stale_revision", "fields": ["revision"]},
    )


def validation_error(detail: str, *fields: str, status: int = 422) -> OdinError:
    return OdinError(
        ErrorCode.validation_failed,
        detail,
        status=status,
        extra={"fields": list(fields)},
    )
