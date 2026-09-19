"""Education capability and revisioned cost-center administration routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.db import get_db_type
from core.db_compat import execute_insert_returning_id
from core.errors import ErrorCode, OdinError
from modules.organizations.education_access import (
    CURSOR_ORDER_VERSION,
    capabilities_for,
    decode_cursor,
    normalize_center_key,
    require_education_capability_principal,
    require_education_principal,
    require_tenant_admin,
    validate_cursor,
)
from modules.organizations.education_admin_support import (
    center_projection,
    finish_mutation,
    mutation_context,
    page_sorted,
    reload_required,
    validation_error,
)
from modules.organizations.education_schemas import (
    CostCenterCreate,
    CostCenterLifecycle,
    CostCenterUpdate,
    EducationCapabilities,
    GrantReplacement,
    PrinterReplacement,
)


router = APIRouter(prefix="/education", tags=["Education"])
_TERMINAL_SUBMISSION_STATES = ("completed", "failed", "rejected", "cancelled")


def _not_found() -> OdinError:
    return OdinError(ErrorCode.not_found, "Cost center not found", status=404)


def _center(db: Session, center_id: int):
    row = db.execute(
        text("SELECT * FROM education_cost_centers WHERE id=:id"), {"id": center_id}
    ).fetchone()
    if not row:
        raise _not_found()
    return row


def _admin_org_for_center(principal: dict, center, requested_org_id: int | None) -> int:
    org_id = require_tenant_admin(principal, requested_org_id)
    if int(center.org_id) != org_id:
        raise _not_found()
    return org_id


def _active_grant_set(db: Session, center_id: int) -> list[dict]:
    rows = db.execute(
        text(
            "SELECT user_id, role FROM education_cost_center_grants "
            "WHERE cost_center_id=:id AND state='active' ORDER BY user_id, role"
        ),
        {"id": center_id},
    ).fetchall()
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(int(row.user_id), []).append(row.role)
    return [
        {"user_id": user_id, "roles": sorted(roles)}
        for user_id, roles in sorted(grouped.items())
    ]


def _active_printer_set(db: Session, center_id: int) -> list[int]:
    return [
        int(row.printer_id)
        for row in db.execute(
            text(
                "SELECT printer_id FROM education_cost_center_printers "
                "WHERE cost_center_id=:id AND state='active' ORDER BY printer_id"
            ),
            {"id": center_id},
        ).fetchall()
    ]


def _mutation_details(before, after, *, reason: str | None = None) -> dict:
    details = {"before": before, "after": after}
    if reason is not None:
        details["reason"] = reason
    return details


@router.get("/capabilities", response_model=EducationCapabilities)
async def get_education_capabilities(
    principal: dict = Depends(require_education_capability_principal()),
    db: Session = Depends(get_db),
):
    return capabilities_for(db, principal)


@router.get("/readiness")
async def get_education_readiness(
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    """Return factual, secret-free tenant POC readiness signals."""
    effective_org = require_tenant_admin(principal, org_id)
    mode = db.execute(
        text("SELECT value FROM system_config WHERE key='education_mode'")
    ).fetchone()
    oidc = db.execute(text("SELECT * FROM oidc_config WHERE id=1")).fetchone()
    oidc_map = oidc._mapping if oidc else {}
    provider = oidc_map.get("provider_type") or "microsoft"
    discovery_ready = bool(oidc_map.get("discovery_url")) or provider in {"microsoft", "google"}
    oidc_ready = bool(
        oidc_map.get("is_enabled")
        and oidc_map.get("client_id")
        and oidc_map.get("client_secret_encrypted")
        and oidc_map.get("default_group_id") == effective_org
        and discovery_ready
        and (provider != "google" or oidc_map.get("allowed_domains"))
    )
    classroom = db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"),
        {"org_id": effective_org},
    ).fetchone()
    from modules.organizations.classroom_service import connection_status

    counts = db.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM education_cost_centers WHERE org_id=:org_id AND state='active') centers, "
            "(SELECT COUNT(*) FROM education_cost_center_grants WHERE org_id=:org_id AND state='active' AND role='student') students, "
            "(SELECT COUNT(*) FROM education_cost_center_grants WHERE org_id=:org_id AND state='active' AND role='manager') managers, "
            "(SELECT COUNT(*) FROM education_cost_center_printers WHERE org_id=:org_id AND state='active') printers"
        ),
        {"org_id": effective_org},
    ).one()
    return {
        "education_license": True,
        "education_mode": bool(mode and mode.value == "true"),
        "oidc": {
            "ready": oidc_ready,
            "provider": provider,
            "enabled": bool(oidc_map.get("is_enabled")),
        },
        "classroom": connection_status(classroom),
        "pilot": {
            "active_centers": int(counts.centers),
            "student_grants": int(counts.students),
            "manager_grants": int(counts.managers),
            "printer_entitlements": int(counts.printers),
        },
        "backup": {
            "database_backend": get_db_type(),
            "verified_workflow_available": True,
        },
    }


@router.get("/cost-centers")
async def list_cost_centers(
    org_id: int | None = None,
    include_archived: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    if principal.get("role") == "admin":
        effective_org = require_tenant_admin(principal, org_id)
        params = {"org_id": effective_org}
        statement = text(
            "SELECT c.* FROM education_cost_centers c WHERE c.org_id=:org_id"
        ) if include_archived else text(
            "SELECT c.* FROM education_cost_centers c "
            "WHERE c.org_id=:org_id AND c.state='active'"
        )
    else:
        effective_org = principal.get("group_id")
        if effective_org is None:
            return {"items": [], "next_cursor": None}
        if org_id is not None and int(org_id) != int(effective_org):
            raise _not_found()
        params = {"org_id": effective_org, "user_id": principal["id"]}
        statement = text(
            "SELECT c.* FROM education_cost_centers c WHERE c.org_id=:org_id "
            "AND EXISTS (SELECT 1 FROM education_cost_center_grants g "
            "WHERE g.cost_center_id=c.id AND g.org_id=c.org_id AND g.user_id=:user_id "
            "AND g.state='active')"
        ) if include_archived else text(
            "SELECT c.* FROM education_cost_centers c WHERE c.org_id=:org_id "
            "AND EXISTS (SELECT 1 FROM education_cost_center_grants g "
            "WHERE g.cost_center_id=c.id AND g.org_id=c.org_id AND g.user_id=:user_id "
            "AND g.state='active') AND c.state='active'"
        )
    last = None
    if cursor:
        payload = decode_cursor(cursor)
        expected = {
            "kind": "centers",
            "org_id": int(effective_org),
            "include_archived": include_archived,
            "limit": limit,
            "order_version": CURSOR_ORDER_VERSION,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise OdinError(ErrorCode.invalid_cursor, "Education cursor does not match request", status=400)
        last = payload.get("last")
        if not isinstance(last, list):
            raise OdinError(ErrorCode.invalid_cursor, "Invalid Education cursor", status=400)
    rows = db.execute(statement, params).fetchall()
    page, next_cursor = page_sorted(
        rows,
        last=last,
        limit=limit,
        key=lambda row: (row.name_key, int(row.id)),
        cursor_payload={
            "kind": "centers", "org_id": int(effective_org),
            "include_archived": include_archived, "limit": limit,
            "order_version": CURSOR_ORDER_VERSION,
        },
    )
    return {"items": [center_projection(db, row) for row in page], "next_cursor": next_cursor}


@router.get("/cost-centers/{center_id}")
async def get_cost_center(
    center_id: int,
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    center = _center(db, center_id)
    _admin_org_for_center(principal, center, org_id)
    return center_projection(db, center)


@router.post("/cost-centers", status_code=201)
async def create_cost_center(
    body: CostCenterCreate,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    org_id = require_tenant_admin(principal, body.org_id)
    command_id, digest, replay = mutation_context(
        db, body, principal, org_id, "cost_center.create"
    )
    if replay is not None:
        return replay
    if not db.execute(
        text("SELECT 1 FROM groups WHERE id=:id AND is_org IS TRUE"), {"id": org_id}
    ).fetchone():
        db.rollback()
        raise OdinError(ErrorCode.not_found, "Organization not found", status=404)
    try:
        center_id = execute_insert_returning_id(
            db,
            "INSERT INTO education_cost_centers "
            "(org_id, name_key, code_key, display_name, code, description, created_by) "
            "VALUES (:org_id, :name_key, :code_key, :name, :code, :description, :actor_id)",
            {
                "org_id": org_id, "name_key": normalize_center_key(body.name),
                "code_key": normalize_center_key(body.code), "name": body.name.strip(),
                "code": body.code.strip(), "description": body.description,
                "actor_id": principal["id"],
            },
        )
        result = center_projection(db, _center(db, center_id))
        finish_mutation(
            db, principal=principal, org_id=org_id, action="cost_center.create",
            command_id=command_id, request_digest=digest, resource_id=center_id,
            revision=1, details=_mutation_details(None, result), result=result,
        )
        db.commit()
        return result
    except IntegrityError as exc:
        db.rollback()
        raise OdinError(
            ErrorCode.resource_conflict, "Cost center name or code already exists",
            status=409, extra={"fields": ["name", "code"]},
        ) from exc


@router.patch("/cost-centers/{center_id}")
async def update_cost_center(
    center_id: int,
    body: CostCenterUpdate,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    center = _center(db, center_id)
    org_id = _admin_org_for_center(principal, center, body.org_id)
    command_id, digest, replay = mutation_context(
        db, body, principal, org_id, "cost_center.update"
    )
    if replay is not None:
        return replay
    before = center_projection(db, center)
    values = {
        "id": center_id, "org_id": org_id, "revision": body.revision,
        "name": body.name.strip() if body.name is not None else center.display_name,
        "name_key": normalize_center_key(body.name) if body.name is not None else center.name_key,
        "code": body.code.strip() if body.code is not None else center.code,
        "code_key": normalize_center_key(body.code) if body.code is not None else center.code_key,
        "description": body.description if body.description is not None else center.description,
    }
    try:
        changed = db.execute(
            text(
                "UPDATE education_cost_centers SET display_name=:name, name_key=:name_key, "
                "code=:code, code_key=:code_key, description=:description, "
                "revision=revision+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:id AND org_id=:org_id AND revision=:revision"
            ),
            values,
        )
        if changed.rowcount != 1:
            db.rollback()
            raise reload_required()
        after = center_projection(db, _center(db, center_id))
        finish_mutation(
            db, principal=principal, org_id=org_id, action="cost_center.update",
            command_id=command_id, request_digest=digest, resource_id=center_id,
            revision=after["revision"], details=_mutation_details(before, after), result=after,
        )
        db.commit()
        return after
    except IntegrityError as exc:
        db.rollback()
        raise OdinError(
            ErrorCode.resource_conflict, "Cost center name or code already exists",
            status=409, extra={"fields": ["name", "code"]},
        ) from exc


async def _change_center_lifecycle(
    center_id: int, body: CostCenterLifecycle, principal: dict, db: Session,
    *, target_state: str,
):
    center = _center(db, center_id)
    org_id = _admin_org_for_center(principal, center, body.org_id)
    action = f"cost_center.{target_state}"
    command_id, digest, replay = mutation_context(db, body, principal, org_id, action)
    if replay is not None:
        return replay
    if center.state == target_state:
        db.rollback()
        raise validation_error(f"Cost center is already {target_state}", "state", status=409)
    if target_state == "archived":
        nonterminal_statement = text(
            "SELECT 1 FROM education_submissions WHERE cost_center_id=:center_id "
            "AND status NOT IN :terminal_states LIMIT 1"
        ).bindparams(bindparam("terminal_states", expanding=True))
        if db.execute(
            nonterminal_statement,
            {"center_id": center_id, "terminal_states": _TERMINAL_SUBMISSION_STATES},
        ).fetchone():
            db.rollback()
            raise OdinError(
                ErrorCode.invalid_state_transition, "Cost center has nonterminal submissions",
                status=409, extra={"fields": ["state"]},
            )
    before = center_projection(db, center)
    changed = db.execute(
        text(
            "UPDATE education_cost_centers SET state=:state, revision=revision+1, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=:id AND org_id=:org_id AND revision=:revision"
        ),
        {"state": target_state, "id": center_id, "org_id": org_id, "revision": body.revision},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise reload_required()
    after = center_projection(db, _center(db, center_id))
    finish_mutation(
        db, principal=principal, org_id=org_id, action=action,
        command_id=command_id, request_digest=digest, resource_id=center_id,
        revision=after["revision"],
        details=_mutation_details(before, after, reason=body.reason), result=after,
    )
    db.commit()
    return after


@router.post("/cost-centers/{center_id}/archive")
async def archive_cost_center(
    center_id: int,
    body: CostCenterLifecycle,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    return await _change_center_lifecycle(center_id, body, principal, db, target_state="archived")


@router.post("/cost-centers/{center_id}/reopen")
async def reopen_cost_center(
    center_id: int,
    body: CostCenterLifecycle,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    return await _change_center_lifecycle(center_id, body, principal, db, target_state="active")


@router.get("/cost-centers/{center_id}/grants")
async def list_cost_center_grants(
    center_id: int,
    state: Literal["active", "revoked", "all"] = "active",
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    center = _center(db, center_id)
    effective_org = _admin_org_for_center(principal, center, org_id)
    last = validate_cursor(
        cursor, kind="grants", center_id=center_id, state=state,
        limit=limit, revision=int(center.revision),
    )
    statement = text(
        "SELECT g.id, g.user_id, u.username, NULL AS display_name, g.role, g.state, "
        "g.granted_at, g.revoked_at FROM education_cost_center_grants g "
        "JOIN users u ON u.id=g.user_id WHERE g.cost_center_id=:center_id "
        "AND g.org_id=:org_id"
    ) if state == "all" else text(
        "SELECT g.id, g.user_id, u.username, NULL AS display_name, g.role, g.state, "
        "g.granted_at, g.revoked_at FROM education_cost_center_grants g "
        "JOIN users u ON u.id=g.user_id WHERE g.cost_center_id=:center_id "
        "AND g.org_id=:org_id AND g.state=:state"
    )
    rows = db.execute(
        statement,
        {"center_id": center_id, "org_id": effective_org, "state": state},
    ).fetchall()
    page, next_cursor = page_sorted(
        rows, last=last, limit=limit,
        key=lambda row: (
            0 if row.state == "active" else 1, row.role,
            normalize_center_key(row.username), int(row.id),
        ),
        cursor_payload={
            "kind": "grants", "center_id": center_id, "state": state,
            "limit": limit, "order_version": CURSOR_ORDER_VERSION,
            "center_revision": int(center.revision),
        },
    )
    return {
        "items": [
            {
                "grant_id": int(row.id), "user_id": int(row.user_id),
                "username": row.username, "display_name": row.display_name,
                "role": row.role, "state": row.state,
                "granted_at": row.granted_at, "revoked_at": row.revoked_at,
            }
            for row in page
        ],
        "next_cursor": next_cursor, "center_revision": int(center.revision),
    }


@router.put("/cost-centers/{center_id}/grants")
async def replace_cost_center_grants(
    center_id: int,
    body: GrantReplacement,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    center = _center(db, center_id)
    org_id = _admin_org_for_center(principal, center, body.org_id)
    user_ids = [item.user_id for item in body.grants]
    if len(user_ids) != len(set(user_ids)):
        raise validation_error("Exactly one grant entry is allowed per user", "grants")
    command_id, digest, replay = mutation_context(
        db, body, principal, org_id, "cost_center.grants.replace"
    )
    if replay is not None:
        return replay
    if user_ids:
        query = text(
            "SELECT id FROM users WHERE id IN :ids AND group_id=:org_id AND is_active IS TRUE"
        ).bindparams(bindparam("ids", expanding=True))
        valid = {int(row.id) for row in db.execute(query, {"ids": user_ids, "org_id": org_id})}
        if valid != set(user_ids):
            db.rollback()
            raise validation_error("Grant contains inactive or cross-tenant user", "grants")
    requested = sorted((item.user_id, role) for item in body.grants for role in item.roles)
    before = _active_grant_set(db, center_id)
    changed = db.execute(
        text(
            "UPDATE education_cost_centers SET revision=revision+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=:id AND org_id=:org_id AND revision=:revision"
        ),
        {"id": center_id, "org_id": org_id, "revision": body.revision},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise reload_required()
    db.execute(
        text(
            "UPDATE education_cost_center_grants SET state='revoked', revoked_by=:actor, "
            "revoked_at=CURRENT_TIMESTAMP WHERE cost_center_id=:center_id AND state='active'"
        ),
        {"actor": principal["id"], "center_id": center_id},
    )
    for user_id, role in requested:
        db.execute(
            text(
                "INSERT INTO education_cost_center_grants "
                "(org_id, cost_center_id, user_id, role, state, granted_by) "
                "VALUES (:org_id, :center_id, :user_id, :role, 'active', :actor) "
                "ON CONFLICT (cost_center_id, user_id, role) DO UPDATE SET "
                "state='active', granted_by=:actor, granted_at=CURRENT_TIMESTAMP, "
                "revoked_by=NULL, revoked_at=NULL"
            ),
            {"org_id": org_id, "center_id": center_id, "user_id": user_id,
             "role": role, "actor": principal["id"]},
        )
    after = _active_grant_set(db, center_id)
    result = {"id": center_id, "revision": body.revision + 1, "grants": after}
    finish_mutation(
        db, principal=principal, org_id=org_id, action="cost_center.grants.replace",
        command_id=command_id, request_digest=digest, resource_id=center_id,
        revision=result["revision"], details=_mutation_details(before, after), result=result,
    )
    db.commit()
    return result


def _can_read_printers(
    db: Session, principal: dict, center, requested_org_id: int | None
) -> tuple[int, bool]:
    if principal.get("role") == "admin":
        org_id = require_tenant_admin(principal, requested_org_id)
        if org_id != int(center.org_id):
            raise _not_found()
        return org_id, True
    org_id = principal.get("group_id")
    if org_id != int(center.org_id) or requested_org_id is not None:
        raise _not_found()
    grant = db.execute(
        text(
            "SELECT 1 FROM education_cost_center_grants WHERE org_id=:org_id "
            "AND cost_center_id=:center_id AND user_id=:user_id "
            "AND role='manager' AND state='active'"
        ),
        {"org_id": org_id, "center_id": center.id, "user_id": principal["id"]},
    ).fetchone()
    if not grant:
        raise _not_found()
    return int(org_id), False


@router.get("/cost-centers/{center_id}/printers")
async def list_cost_center_printers(
    center_id: int,
    state: Literal["active", "revoked", "all"] = "active",
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    center = _center(db, center_id)
    effective_org, is_admin = _can_read_printers(db, principal, center, org_id)
    if not is_admin and state != "active":
        raise OdinError(
            ErrorCode.permission_denied, "Revoked printer history requires tenant admin", status=403
        )
    last = validate_cursor(
        cursor, kind="printers", center_id=center_id, state=state,
        limit=limit, revision=int(center.revision),
    )
    statement = text(
        "SELECT e.id, e.printer_id, p.name, p.display_order, p.machine_type, p.api_type, "
        "e.state, e.granted_at, e.revoked_at FROM education_cost_center_printers e "
        "JOIN printers p ON p.id=e.printer_id WHERE e.cost_center_id=:center_id "
        "AND e.org_id=:org_id"
    ) if state == "all" else text(
        "SELECT e.id, e.printer_id, p.name, p.display_order, p.machine_type, p.api_type, "
        "e.state, e.granted_at, e.revoked_at FROM education_cost_center_printers e "
        "JOIN printers p ON p.id=e.printer_id WHERE e.cost_center_id=:center_id "
        "AND e.org_id=:org_id AND e.state=:state"
    )
    rows = db.execute(
        statement,
        {"center_id": center_id, "org_id": effective_org, "state": state},
    ).fetchall()
    page, next_cursor = page_sorted(
        rows, last=last, limit=limit,
        key=lambda row: (
            0 if row.state == "active" else 1, int(row.display_order or 0),
            normalize_center_key(row.name), int(row.id),
        ),
        cursor_payload={
            "kind": "printers", "center_id": center_id, "state": state,
            "limit": limit, "order_version": CURSOR_ORDER_VERSION,
            "center_revision": int(center.revision),
        },
    )
    return {
        "items": [
            {
                "entitlement_id": int(row.id), "printer_id": int(row.printer_id),
                "name": row.name, "machine_type": row.machine_type,
                "api_type": row.api_type, "state": row.state,
                "granted_at": row.granted_at, "revoked_at": row.revoked_at,
            }
            for row in page
        ],
        "next_cursor": next_cursor, "center_revision": int(center.revision),
    }


@router.put("/cost-centers/{center_id}/printers")
async def replace_cost_center_printers(
    center_id: int,
    body: PrinterReplacement,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    center = _center(db, center_id)
    org_id = _admin_org_for_center(principal, center, body.org_id)
    requested = sorted(body.printer_ids)
    command_id, digest, replay = mutation_context(
        db, body, principal, org_id, "cost_center.printers.replace"
    )
    if replay is not None:
        return replay
    if requested:
        query = text(
            "SELECT id FROM printers WHERE id IN :ids AND org_id=:org_id "
            "AND is_active IS TRUE AND (shared IS NULL OR shared IS FALSE)"
        ).bindparams(bindparam("ids", expanding=True))
        valid = {int(row.id) for row in db.execute(query, {"ids": requested, "org_id": org_id})}
        if valid != set(requested):
            db.rollback()
            raise validation_error("Printer is inactive, shared, or cross-tenant", "printer_ids")
    before = _active_printer_set(db, center_id)
    changed = db.execute(
        text(
            "UPDATE education_cost_centers SET revision=revision+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=:id AND org_id=:org_id AND revision=:revision"
        ),
        {"id": center_id, "org_id": org_id, "revision": body.revision},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise reload_required()
    db.execute(
        text(
            "UPDATE education_cost_center_printers SET state='revoked', revoked_by=:actor, "
            "revoked_at=CURRENT_TIMESTAMP WHERE cost_center_id=:center_id AND state='active'"
        ),
        {"actor": principal["id"], "center_id": center_id},
    )
    for printer_id in requested:
        db.execute(
            text(
                "INSERT INTO education_cost_center_printers "
                "(org_id, cost_center_id, printer_id, state, granted_by) "
                "VALUES (:org_id, :center_id, :printer_id, 'active', :actor) "
                "ON CONFLICT (cost_center_id, printer_id) DO UPDATE SET "
                "state='active', granted_by=:actor, granted_at=CURRENT_TIMESTAMP, "
                "revoked_by=NULL, revoked_at=NULL"
            ),
            {"org_id": org_id, "center_id": center_id,
             "printer_id": printer_id, "actor": principal["id"]},
        )
    after = _active_printer_set(db, center_id)
    result = {"id": center_id, "revision": body.revision + 1, "printer_ids": after}
    finish_mutation(
        db, principal=principal, org_id=org_id, action="cost_center.printers.replace",
        command_id=command_id, request_digest=digest, resource_id=center_id,
        revision=result["revision"], details=_mutation_details(before, after), result=result,
    )
    db.commit()
    return result
