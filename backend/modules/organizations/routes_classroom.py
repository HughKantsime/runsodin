"""Tenant-admin Google Classroom connection, preview, and explicit roster import."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import execute_insert_returning_id
from core.dependencies import log_audit
from core.errors import ErrorCode, OdinError
from modules.organizations.classroom_service import (
    ClassroomError,
    complete_connection,
    configure_connection,
    connection_status,
    create_connect_url,
    disconnect,
    get_roster,
    list_courses,
)
from modules.organizations.education_access import (
    normalize_center_key,
    require_education_principal,
    require_tenant_admin,
)
from modules.organizations.education_admin_support import finish_mutation, mutation_context


router = APIRouter(prefix="/education/classroom", tags=["Education", "Classroom"])


class ClassroomConfigInput(BaseModel):
    org_id: int | None = None
    client_id: str = Field(min_length=5, max_length=500)
    client_secret: str | None = Field(default=None, min_length=5, max_length=1000)
    allowed_domains: str = Field(min_length=3, max_length=500)

    @field_validator("client_id", "allowed_domains")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def valid_domains(cls, value: str) -> str:
        domains = [part.strip().lower() for part in value.split(",") if part.strip()]
        if not domains or any("@" in part or "." not in part for part in domains):
            raise ValueError("must contain valid comma-separated Workspace domains")
        return ",".join(dict.fromkeys(domains))


class ClassroomImportInput(BaseModel):
    org_id: int | None = None
    cost_center_id: int | None = Field(default=None, gt=0)
    update_metadata: bool = False
    command_id: UUID


def _raise_service(error: ClassroomError):
    raise OdinError(
        ErrorCode.upstream_unavailable if error.status >= 500 else ErrorCode.validation_failed,
        error.message,
        status=error.status,
        extra={"reason": error.code},
    ) from error


def _connection(db: Session, org_id: int):
    return db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": org_id}
    ).fetchone()


def _org(principal: dict, requested: int | None) -> int:
    return require_tenant_admin(principal, requested)


def _record_failure_audit(
    db: Session,
    *,
    action: str,
    principal: dict,
    org_id: int,
    reason: str,
) -> None:
    """Persist a sanitized failure fact after the failed transaction rolls back."""
    log_audit(
        db,
        action,
        "classroom",
        details={
            "org_id": org_id,
            "actor_id": principal["id"],
            "reason": reason[:100],
        },
    )
    db.commit()


@router.get("/status")
async def classroom_status(
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    effective_org = _org(principal, org_id)
    return connection_status(_connection(db, effective_org))


@router.put("/config")
async def save_classroom_config(
    body: ClassroomConfigInput,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    org_id = _org(principal, body.org_id)
    try:
        result = configure_connection(
            db,
            org_id=org_id,
            admin_id=principal["id"],
            client_id=body.client_id,
            client_secret=body.client_secret,
            allowed_domains=body.allowed_domains,
        )
    except ClassroomError as error:
        _raise_service(error)
    log_audit(db, "classroom_config_updated", "classroom", details={"org_id": org_id})
    db.commit()
    return result


@router.post("/connect-url")
async def classroom_connect_url(
    request: Request,
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    effective_org = _org(principal, org_id)
    redirect_uri = f"{str(request.base_url).rstrip('/')}/api/education/classroom/callback"
    try:
        url = create_connect_url(
            db,
            org_id=effective_org,
            admin_id=principal["id"],
            redirect_uri=redirect_uri,
        )
    except ClassroomError as error:
        _raise_service(error)
    return {"authorization_url": url, "redirect_uri": redirect_uri}


@router.get("/callback")
async def classroom_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    if error or not state or not code:
        _record_failure_audit(
            db,
            action="classroom_connect_failed",
            principal=principal,
            org_id=int(principal["group_id"]),
            reason="authorization_failed",
        )
        return RedirectResponse("/education?classroom=authorization_failed", status_code=302)
    try:
        result = await complete_connection(db, state=state, code=code, principal=principal)
        log_audit(db, "classroom_connected", "classroom", details={"org_id": principal.get("group_id")})
        db.commit()
        return RedirectResponse("/education?classroom=connected", status_code=302)
    except ClassroomError as service_error:
        db.rollback()
        _record_failure_audit(
            db,
            action="classroom_connect_failed",
            principal=principal,
            org_id=int(principal["group_id"]),
            reason=service_error.code,
        )
        return RedirectResponse(
            f"/education?classroom={service_error.code}", status_code=302
        )


@router.post("/disconnect")
async def classroom_disconnect(
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    effective_org = _org(principal, org_id)
    result = disconnect(db, effective_org)
    log_audit(db, "classroom_disconnected", "classroom", details={"org_id": effective_org})
    db.commit()
    return result


@router.get("/courses")
async def classroom_courses(
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    effective_org = _org(principal, org_id)
    try:
        return {"items": await list_courses(db, effective_org)}
    except ClassroomError as error:
        _raise_service(error)


def _desired_roster(roster: dict) -> dict[str, dict]:
    desired: dict[str, dict] = {}
    for role, key in (("manager", "teachers"), ("student", "students")):
        for member in roster[key]:
            item = desired.setdefault(
                member["email"],
                {"email": member["email"], "name": member["name"], "provider_user_id": member["provider_user_id"], "roles": set()},
            )
            if item["provider_user_id"] != member["provider_user_id"]:
                raise ClassroomError("classroom_identity_conflict", "Classroom returned conflicting identities", 422)
            item["roles"].add(role)
    return desired


def _current_roster(db: Session, org_id: int, center_id: int | None) -> dict[str, set[str]]:
    if center_id is None:
        return {}
    rows = db.execute(
        text(
            "SELECT LOWER(u.email) email, g.role FROM education_cost_center_grants g "
            "JOIN users u ON u.id=g.user_id WHERE g.org_id=:org_id "
            "AND g.cost_center_id=:center_id AND g.state='active'"
        ),
        {"org_id": org_id, "center_id": center_id},
    ).fetchall()
    current: dict[str, set[str]] = {}
    for row in rows:
        current.setdefault(row.email, set()).add(row.role)
    return current


@router.get("/courses/{course_id}/preview")
async def classroom_course_preview(
    course_id: str,
    org_id: int | None = None,
    principal: dict = Depends(require_education_principal()),
    db: Session = Depends(get_db),
):
    effective_org = _org(principal, org_id)
    try:
        roster = await get_roster(db, effective_org, course_id)
        desired = _desired_roster(roster)
    except ClassroomError as error:
        db.rollback()
        _record_failure_audit(
            db,
            action="classroom_preview_failed",
            principal=principal,
            org_id=effective_org,
            reason=error.code,
        )
        _raise_service(error)
    mapping = db.execute(
        text("SELECT * FROM classroom_course_mappings WHERE org_id=:org_id AND provider_course_id=:course_id"),
        {"org_id": effective_org, "course_id": course_id},
    ).fetchone()
    center_id = int(mapping.cost_center_id) if mapping else None
    current = _current_roster(db, effective_org, center_id)
    added = sorted(email for email, item in desired.items() if current.get(email) != item["roles"])
    removed = sorted(email for email in current if email not in desired)
    return {
        **roster,
        "mapping": {"cost_center_id": center_id, "last_imported_at": mapping.last_imported_at} if mapping else None,
        "diff": {"added_or_changed": added, "removed": removed, "unchanged": len(desired) - len(added)},
    }


def _username_for(db: Session, email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@", 1)[0].lower()) or "student"
    candidate, number = base[:80], 1
    while db.execute(text("SELECT 1 FROM users WHERE username=:username"), {"username": candidate}).fetchone():
        suffix = str(number)
        candidate = f"{base[:80-len(suffix)]}{suffix}"
        number += 1
    return candidate


def _validate_or_create_users(db: Session, org_id: int, desired: dict[str, dict]) -> dict[str, int]:
    connection = _connection(db, org_id)
    domains = {part.strip().lower() for part in (connection.allowed_domains or "").split(",") if part.strip()}
    resolved: dict[str, int] = {}
    for email, member in sorted(desired.items()):
        if domains and email.rsplit("@", 1)[-1] not in domains:
            raise ClassroomError("classroom_member_domain_invalid", "Roster contains a member outside the configured Workspace domain", 422)
        matches = db.execute(text("SELECT * FROM users WHERE LOWER(email)=:email ORDER BY id"), {"email": email}).fetchall()
        if len(matches) > 1:
            raise ClassroomError("classroom_identity_conflict", "Roster email matches multiple ODIN users", 409)
        if matches:
            user = matches[0]
            if int(user.group_id or -1) != org_id or not bool(user.is_active):
                raise ClassroomError("classroom_identity_conflict", "Roster email conflicts with an existing ODIN identity", 409)
            if user.oidc_subject is not None and (
                user.oidc_issuer not in {"https://accounts.google.com", "accounts.google.com"}
                or user.oidc_subject != member["provider_user_id"]
            ):
                raise ClassroomError("classroom_identity_conflict", "Roster email is bound to a different login identity", 409)
            user_id = int(user.id)
        else:
            user_id = execute_insert_returning_id(
                db,
                "INSERT INTO users (username, email, password_hash, role, is_active, group_id) "
                "VALUES (:username, :email, '', 'viewer', 1, :org_id)",
                {"username": _username_for(db, email), "email": email, "org_id": org_id},
            )
        resolved[email] = user_id
    return resolved


def _mapped_or_new_center(db: Session, org_id: int, principal: dict, body: ClassroomImportInput, roster: dict) -> tuple[int, bool]:
    course = roster["course"]
    mapping = db.execute(
        text("SELECT * FROM classroom_course_mappings WHERE org_id=:org_id AND provider_course_id=:course_id"),
        {"org_id": org_id, "course_id": course["id"]},
    ).fetchone()
    if mapping:
        if body.cost_center_id is not None and int(mapping.cost_center_id) != body.cost_center_id:
            raise ClassroomError("classroom_mapping_conflict", "Course is already mapped to another cost center", 409)
        return int(mapping.cost_center_id), False
    if body.cost_center_id is not None:
        center = db.execute(
            text("SELECT id FROM education_cost_centers WHERE id=:id AND org_id=:org_id AND state='active'"),
            {"id": body.cost_center_id, "org_id": org_id},
        ).fetchone()
        occupied = db.execute(
            text("SELECT 1 FROM classroom_course_mappings WHERE org_id=:org_id AND cost_center_id=:id"),
            {"id": body.cost_center_id, "org_id": org_id},
        ).fetchone()
        if not center or occupied:
            raise ClassroomError("classroom_mapping_conflict", "Selected cost center cannot be mapped", 409)
        center_id = body.cost_center_id
    else:
        suffix = re.sub(r"[^A-Za-z0-9-]", "", course["id"])[-8:] or "course"
        base_name = course["name"].strip()[:180]
        name = base_name
        number = 1
        while db.execute(text("SELECT 1 FROM education_cost_centers WHERE org_id=:org_id AND name_key=:key"), {"org_id": org_id, "key": normalize_center_key(name)}).fetchone():
            number += 1
            name = f"{base_name[:170]} ({number})"
        code = (course.get("section") or f"GC-{suffix}").strip()[:90]
        while db.execute(text("SELECT 1 FROM education_cost_centers WHERE org_id=:org_id AND code_key=:key"), {"org_id": org_id, "key": normalize_center_key(code)}).fetchone():
            code = f"{code[:80]}-{suffix}"
        center_id = execute_insert_returning_id(
            db,
            "INSERT INTO education_cost_centers (org_id, name_key, code_key, display_name, code, description, created_by) "
            "VALUES (:org_id, :name_key, :code_key, :name, :code, :description, :actor)",
            {"org_id": org_id, "name_key": normalize_center_key(name), "code_key": normalize_center_key(code), "name": name, "code": code, "description": course.get("description") or "Imported from Google Classroom", "actor": principal["id"]},
        )
    db.execute(
        text("INSERT INTO classroom_course_mappings (org_id, provider_course_id, cost_center_id, course_name, course_section, course_state) VALUES (:org_id, :course_id, :center_id, :name, :section, :state)"),
        {"org_id": org_id, "course_id": course["id"], "center_id": center_id, "name": course["name"], "section": course.get("section"), "state": course.get("course_state")},
    )
    return center_id, True


@router.post("/courses/{course_id}/import")
async def import_classroom_course(
    course_id: str,
    body: ClassroomImportInput,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    org_id = _org(principal, body.org_id)
    claimed_command: tuple[str, str] | None = None
    try:
        roster = await get_roster(db, org_id, course_id)
        desired = _desired_roster(roster)
        # Establish the request transaction before claim_command opens its
        # savepoint, so a validation failure rolls the idempotency claim back.
        if not _connection(db, org_id):
            raise ClassroomError("classroom_not_configured", "Google Classroom is not configured", 409)
        command_id, digest, replay = mutation_context(db, body, principal, org_id, "classroom.roster.import")
        if replay is not None:
            return replay
        claimed_command = (command_id, digest)
        users = _validate_or_create_users(db, org_id, desired)
        center_id, created = _mapped_or_new_center(db, org_id, principal, body, roster)
        center = db.execute(text("SELECT * FROM education_cost_centers WHERE id=:id AND org_id=:org_id"), {"id": center_id, "org_id": org_id}).one()
        before = _current_roster(db, org_id, center_id)
        before_grants = {
            (email, role) for email, roles in before.items() for role in roles
        }
        desired_grants = {
            (email, role)
            for email, item in desired.items()
            for role in item["roles"]
        }
        if body.update_metadata and not created:
            db.execute(
                text("UPDATE education_cost_centers SET display_name=:name, name_key=:name_key, description=:description WHERE id=:id AND org_id=:org_id"),
                {"name": roster["course"]["name"], "name_key": normalize_center_key(roster["course"]["name"]), "description": roster["course"].get("description") or center.description, "id": center_id, "org_id": org_id},
            )
        db.execute(
            text("UPDATE education_cost_centers SET revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE id=:id AND org_id=:org_id"),
            {"id": center_id, "org_id": org_id},
        )
        db.execute(
            text("UPDATE education_cost_center_grants SET state='revoked', revoked_by=:actor, revoked_at=CURRENT_TIMESTAMP WHERE org_id=:org_id AND cost_center_id=:center_id AND state='active'"),
            {"actor": principal["id"], "org_id": org_id, "center_id": center_id},
        )
        now = datetime.now(timezone.utc).isoformat()
        for email, member in sorted(desired.items()):
            user_id = users[email]
            for role in sorted(member["roles"]):
                db.execute(
                    text("INSERT INTO education_cost_center_grants (org_id, cost_center_id, user_id, role, state, granted_by) VALUES (:org_id, :center_id, :user_id, :role, 'active', :actor) ON CONFLICT (cost_center_id, user_id, role) DO UPDATE SET state='active', granted_by=:actor, granted_at=CURRENT_TIMESTAMP, revoked_by=NULL, revoked_at=NULL"),
                    {"org_id": org_id, "center_id": center_id, "user_id": user_id, "role": role, "actor": principal["id"]},
                )
            db.execute(
                text("INSERT INTO classroom_roster_identities (org_id, user_id, provider_user_id, normalized_email, state, last_seen_at) VALUES (:org_id, :user_id, :provider_user_id, :email, 'active', :now) ON CONFLICT (org_id, provider_user_id) DO UPDATE SET user_id=:user_id, normalized_email=:email, state='active', last_seen_at=:now, updated_at=CURRENT_TIMESTAMP"),
                {"org_id": org_id, "user_id": user_id, "provider_user_id": member["provider_user_id"], "email": email, "now": now},
            )
        db.execute(
            text("UPDATE classroom_course_mappings SET course_name=:name, course_section=:section, course_state=:state, last_imported_at=:now, updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id AND provider_course_id=:course_id"),
            {"name": roster["course"]["name"], "section": roster["course"].get("section"), "state": roster["course"].get("course_state"), "now": now, "org_id": org_id, "course_id": course_id},
        )
        result = {
            "course_id": course_id,
            "cost_center_id": center_id,
            "created_cost_center": created,
            "teachers": len(roster["teachers"]),
            "students": len(roster["students"]),
            "revision": int(center.revision) + 1,
        }
        finish_mutation(
            db,
            principal=principal,
            org_id=org_id,
            action="classroom.roster.import",
            command_id=command_id,
            request_digest=digest,
            resource_id=center_id,
            revision=result["revision"],
            details={
                "before_count": len(before_grants),
                "after_count": len(desired_grants),
                "added_grant_count": len(desired_grants - before_grants),
                "revoked_grant_count": len(before_grants - desired_grants),
                "unchanged_grant_count": len(before_grants & desired_grants),
                "course_id": course_id,
                "mapping_created": created,
            },
            result=result,
        )
        db.commit()
        return result
    except ClassroomError as error:
        db.rollback()
        if claimed_command:
            db.execute(
                text(
                    "DELETE FROM education_commands WHERE org_id=:org_id AND actor_kind='user' "
                    "AND actor_id=:actor_id AND action='classroom.roster.import' "
                    "AND command_id=:command_id AND request_hash=:request_hash AND state='pending'"
                ),
                {
                    "org_id": org_id,
                    "actor_id": str(principal["id"]),
                    "command_id": claimed_command[0],
                    "request_hash": claimed_command[1],
                },
            )
        _record_failure_audit(
            db,
            action="classroom_import_failed",
            principal=principal,
            org_id=org_id,
            reason=error.code,
        )
        _raise_service(error)
    except IntegrityError as error:
        db.rollback()
        if claimed_command:
            db.execute(
                text(
                    "DELETE FROM education_commands WHERE org_id=:org_id AND actor_kind='user' "
                    "AND actor_id=:actor_id AND action='classroom.roster.import' "
                    "AND command_id=:command_id AND request_hash=:request_hash AND state='pending'"
                ),
                {
                    "org_id": org_id,
                    "actor_id": str(principal["id"]),
                    "command_id": claimed_command[0],
                    "request_hash": claimed_command[1],
                },
            )
        _record_failure_audit(
            db,
            action="classroom_import_failed",
            principal=principal,
            org_id=org_id,
            reason="resource_conflict",
        )
        raise OdinError(ErrorCode.resource_conflict, "Classroom import conflicts with current ODIN data", status=409) from error
