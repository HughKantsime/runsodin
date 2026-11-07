"""Organizations sessions routes — session management, API tokens, quotas, and GDPR."""

from core.rbac import require_scope, handler_enforces_token_scope, require_interactive_session

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from modules.organizations.session_schemas import QuotaUpdateRequest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import execute_insert_returning_id, sql
from core.dependencies import get_current_user, log_audit
from core.errors import ErrorCode, OdinError
from core.rbac import require_role
from core.auth import hash_password
import core.auth as auth_module
from core.quota import _get_quota_usage
from core.rate_limit import limiter
from license_manager import require_feature

from modules.organizations.routes_users import _is_superadmin, _check_org_admin_access

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Session Management ==============

@router.get("/sessions", tags=["Sessions"])
async def list_sessions(request: Request, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List active sessions for the current user."""
    rows = db.execute(text(
        "SELECT s.id, s.token_jti, s.ip_address, s.user_agent, s.created_at, s.last_seen_at "
        "FROM active_sessions s WHERE s.user_id = :uid ORDER BY s.last_seen_at DESC"),
        {"uid": current_user["id"]}).fetchall()

    current_jti = None
    session_cookie = request.cookies.get("session")
    auth_header = request.headers.get("authorization", "")
    raw_token = session_cookie or (auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None)
    if raw_token:
        try:
            import jwt as _jwt
            payload = _jwt.decode(raw_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
            current_jti = payload.get("jti")
        except Exception as e:
            log.debug(f"Failed to decode session token for current-session check: {e}")

    return [{
        "id": r.id,
        "ip_address": r.ip_address,
        "user_agent": r.user_agent,
        "created_at": r.created_at,
        "last_seen_at": r.last_seen_at,
        "is_current": r.token_jti == current_jti,
    } for r in rows]


@router.delete("/sessions/{session_id}", tags=["Sessions"], dependencies=[Depends(require_interactive_session())])
async def revoke_session(session_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke a specific session."""
    row = db.execute(text("SELECT * FROM active_sessions WHERE id = :id AND user_id = :uid"),
                     {"id": session_id, "uid": current_user["id"]}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    parse_dt = lambda s: datetime.fromisoformat(s) if isinstance(s, str) else s
    try:
        created = parse_dt(row.created_at) if isinstance(row.created_at, str) else row.created_at
        expires_at = created + timedelta(hours=24)
    except Exception:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    db.execute(text(f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) VALUES (:jti, :exp){sql.on_conflict_ignore('jti')}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
               {"jti": row.token_jti, "exp": expires_at})
    db.execute(text("DELETE FROM active_sessions WHERE id = :id"), {"id": session_id})
    db.commit()
    return {"status": "ok"}


@router.delete("/sessions", tags=["Sessions"], dependencies=[Depends(require_interactive_session())])
async def revoke_all_sessions(request: Request, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke all sessions except the current one."""
    from core.mfa_login import cancel_pending_enrollments
    cancel_pending_enrollments(db, current_user["id"])
    current_jti = None
    session_cookie = request.cookies.get("session")
    auth_header = request.headers.get("authorization", "")
    raw_token = session_cookie or (auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else None)
    if raw_token:
        try:
            import jwt as _jwt
            payload = _jwt.decode(raw_token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM])
            current_jti = payload.get("jti")
        except Exception as e:
            log.debug(f"Failed to decode session token: {e}")

    rows = db.execute(text("SELECT token_jti, created_at FROM active_sessions WHERE user_id = :uid"),
                      {"uid": current_user["id"]}).fetchall()
    count = 0
    for r in rows:
        if r.token_jti == current_jti:
            continue
        try:
            parse_dt = lambda s: datetime.fromisoformat(s) if isinstance(s, str) else s
            created = parse_dt(r.created_at) if isinstance(r.created_at, str) else r.created_at
            expires_at = created + timedelta(hours=24)
        except Exception:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        db.execute(text(f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) VALUES (:jti, :exp){sql.on_conflict_ignore('jti')}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                   {"jti": r.token_jti, "exp": expires_at})
        count += 1

    db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid AND token_jti != :jti"),
               {"uid": current_user["id"], "jti": current_jti or ""})
    db.commit()
    return {"status": "ok", "revoked": count}


@router.get("/admin/sessions", tags=["Sessions"])
async def admin_list_sessions(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: list all active sessions across all users."""
    if _is_superadmin(current_user):
        rows = db.execute(text(
            "SELECT s.id, s.user_id, u.username, s.ip_address, s.user_agent, s.created_at, s.last_seen_at "
            "FROM active_sessions s JOIN users u ON s.user_id = u.id "
            "ORDER BY s.last_seen_at DESC LIMIT 200")).fetchall()
    else:
        rows = db.execute(text(
            "SELECT s.id, s.user_id, u.username, s.ip_address, s.user_agent, s.created_at, s.last_seen_at "
            "FROM active_sessions s JOIN users u ON s.user_id = u.id "
            "WHERE u.group_id = :gid OR u.group_id IS NULL "
            "ORDER BY s.last_seen_at DESC LIMIT 200"),
            {"gid": current_user["group_id"]}).fetchall()
    return [{
        "id": r.id, "user_id": r.user_id, "username": r.username,
        "ip_address": r.ip_address, "user_agent": r.user_agent,
        "created_at": r.created_at, "last_seen_at": r.last_seen_at,
    } for r in rows]


@router.delete("/admin/sessions/{session_id}", tags=["Sessions"], dependencies=[Depends(require_interactive_session())])
async def admin_revoke_session(session_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: force-revoke any session."""
    row = db.execute(text("SELECT * FROM active_sessions WHERE id = :id"), {"id": session_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    # Org-scoped admin: verify session belongs to a user in their group
    if not _is_superadmin(current_user):
        session_user = db.execute(text("SELECT group_id FROM users WHERE id = :id"), {"id": row.user_id}).fetchone()
        if not session_user or not _check_org_admin_access(current_user, session_user.group_id):
            raise HTTPException(status_code=403, detail="Cannot revoke sessions for users outside your group")

    try:
        parse_dt = lambda s: datetime.fromisoformat(s) if isinstance(s, str) else s
        created = parse_dt(row.created_at) if isinstance(row.created_at, str) else row.created_at
        expires_at = created + timedelta(hours=24)
    except Exception:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    db.execute(text(f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) VALUES (:jti, :exp){sql.on_conflict_ignore('jti')}"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
               {"jti": row.token_jti, "exp": expires_at})
    db.execute(text("DELETE FROM active_sessions WHERE id = :id"), {"id": session_id})
    log_audit(db, "session_revoked_admin", "session", session_id, f"Admin revoked session for user_id={row.user_id}")
    db.commit()
    return {"status": "ok"}


# ============== Scoped API Tokens ==============

from core.api_token_policy import VALID_SCOPES, parse_token_scopes


@router.post("/tokens", tags=["API Tokens"], dependencies=[Depends(require_interactive_session())])
@limiter.limit("10/minute")
async def create_api_token(request: Request, body: dict, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Create a new scoped API token for the current user."""
    import secrets
    raw_name = body.get("name", "")
    if not isinstance(raw_name, str):
        raise HTTPException(status_code=400, detail="Token name must be text")
    name = raw_name.strip()
    scopes = body.get("scopes", [])
    expires_days = body.get("expires_days")

    if not name:
        raise HTTPException(status_code=400, detail="Token name is required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Token name too long")
    if not isinstance(scopes, list) or not scopes or any(not isinstance(scope, str) for scope in scopes):
        raise HTTPException(status_code=400, detail="Select at least one valid token scope")
    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid scopes: {', '.join(invalid)}")
    if "admin" in scopes and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create tokens with admin scope")
    # Phase 2 (v1.9.0): agent-surface mint-time role floor.
    # agent:write grants the 11 advertised write tools without requiring
    # operator role at request time — so the role gate MUST happen here,
    # at mint time. Otherwise a viewer could mint agent:write and bypass
    # operator role (require_any_scope has no role check). viewers may
    # mint agent:read because read routes already floor on viewer anyway.
    if "agent:write" in scopes and current_user["role"] not in ("admin", "operator"):
        raise OdinError(
            ErrorCode.permission_denied,
            "Only operators and admins can mint agent:write tokens",
            status=403,
        )

    if "write:production" in scopes and current_user["role"] not in ("admin", "operator"):
        raise HTTPException(403, "Only operators and admins can mint write:production tokens")

    from core.api_token_policy import camera_scope_combination_error, camera_token_expiry_error, CAMERA_TOKEN_DEFAULT_DAYS
    if "read:cameras" in scopes:
        # Camera tokens live in OBS configs and copied URLs: camera-only, never
        # unbounded, and never longer than the default lifetime.
        refusal = camera_scope_combination_error(scopes) or camera_token_expiry_error(expires_days)
        if refusal:
            raise HTTPException(status_code=422, detail=refusal)
        if expires_days is None:
            expires_days = CAMERA_TOKEN_DEFAULT_DAYS
    expires_at = None
    if expires_days is not None:
        if isinstance(expires_days, bool) or not isinstance(expires_days, int) or expires_days <= 0:
            raise HTTPException(status_code=400, detail="Expiry days must be a positive integer")
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)
        except (OverflowError, ValueError):
            raise HTTPException(status_code=400, detail="Expiry is out of range") from None
    parent_expiry = current_user.get("_token_expires_at")
    if current_user.get("_auth_kind") == "user_api_token" and parent_expiry is not None:
        try:
            parent = datetime.fromisoformat(parent_expiry) if isinstance(parent_expiry, str) else parent_expiry
            if parent.tzinfo is None:
                parent = parent.replace(tzinfo=timezone.utc)
            if parent <= datetime.now(timezone.utc):
                raise ValueError("Expired parent")
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=403, detail="Parent credential expiry is invalid") from None
        expires_at = min(expires_at, parent) if expires_at is not None else parent

    raw_token = f"odin_{secrets.token_urlsafe(32)}"
    token_prefix = raw_token[:10]
    token_hash_val = hash_password(raw_token)

    insert_sql = """INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, scopes, expires_at)
                       VALUES (:user_id, :name, :token_hash, :prefix, :scopes, :expires_at)"""
    params = {"user_id": current_user["id"], "name": name, "token_hash": token_hash_val,
              "prefix": token_prefix, "scopes": json.dumps(scopes), "expires_at": expires_at}
    token_id = execute_insert_returning_id(db, insert_sql, params)
    db.flush()
    log_audit(db, "api_token_created", "api_token", token_id, f"Token '{name}' created")
    db.commit()

    return {
        "id": token_id, "name": name, "token": raw_token, "prefix": token_prefix,
        "scopes": scopes, "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# Lists only the caller's own token metadata (never the secrets), so any
# valid scoped token may list its owner's tokens.
@router.get("/tokens", tags=["API Tokens"], dependencies=[Depends(handler_enforces_token_scope())])
async def list_api_tokens(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List all API tokens for the current user."""
    rows = db.execute(text(
        "SELECT id, name, token_prefix, scopes, expires_at, last_used_at, created_at "
        "FROM api_tokens WHERE user_id = :uid ORDER BY created_at DESC"),
        {"uid": current_user["id"]}).fetchall()
    return [{
        "id": r.id, "name": r.name, "prefix": r.token_prefix,
        "scopes": parse_token_scopes(r.scopes) or [],
        "expires_at": r.expires_at, "last_used_at": r.last_used_at, "created_at": r.created_at,
    } for r in rows]


@router.delete("/tokens/{token_id}", tags=["API Tokens"], dependencies=[Depends(require_interactive_session())])
async def revoke_api_token(token_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke (delete) an API token."""
    row = db.execute(text("SELECT * FROM api_tokens WHERE id = :id AND user_id = :uid"),
                     {"id": token_id, "uid": current_user["id"]}).fetchone()
    if not row:
        if current_user["role"] == "admin":
            row = db.execute(text("SELECT * FROM api_tokens WHERE id = :id"), {"id": token_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")
        target_user = db.execute(text("SELECT group_id FROM users WHERE id = :uid"), {"uid": row.user_id}).fetchone()
        if not target_user or not _check_org_admin_access(current_user, target_user.group_id):
            raise HTTPException(status_code=403, detail="Cannot revoke tokens outside your group")

    db.execute(text("DELETE FROM api_tokens WHERE id = :id"), {"id": token_id})
    log_audit(db, "api_token_revoked", "api_token", token_id, f"Token '{row.name}' revoked")
    db.commit()
    return {"status": "ok"}


# ============== Print Quotas ==============

@router.get("/quotas", tags=["Quotas"])
async def get_my_quota(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get current user's quota config and usage."""
    require_feature("print_quotas")
    period = current_user.get("quota_period") or "monthly"
    # A read: a missing period row means zero usage; charging creates it.
    usage = _get_quota_usage(db, current_user["id"], period, create=False)
    return {
        "quota_grams": current_user.get("quota_grams"),
        "quota_hours": current_user.get("quota_hours"),
        "quota_jobs": current_user.get("quota_jobs"),
        "quota_period": period,
        "usage": {"grams_used": usage["grams_used"], "hours_used": usage["hours_used"], "jobs_used": usage["jobs_used"]},
        "period_key": usage["period_key"],
    }


@router.get("/admin/quotas", tags=["Quotas"])
async def admin_list_quotas(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: list all users' quota config and usage."""
    require_feature("print_quotas")
    if _is_superadmin(current_user):
        users = db.execute(text(
            "SELECT id, username, quota_grams, quota_hours, quota_jobs, quota_period FROM users WHERE is_active IS TRUE"
        )).fetchall()
    else:
        users = db.execute(text(
            "SELECT id, username, quota_grams, quota_hours, quota_jobs, quota_period FROM users "
            "WHERE is_active IS TRUE AND (group_id = :gid OR group_id IS NULL)"),
            {"gid": current_user["group_id"]}).fetchall()
    result = []
    for u in users:
        period = u.quota_period or "monthly"
        usage = _get_quota_usage(db, u.id, period)
        result.append({
            "user_id": u.id, "username": u.username,
            "quota_grams": u.quota_grams, "quota_hours": u.quota_hours,
            "quota_jobs": u.quota_jobs, "quota_period": period,
            "usage": {"grams_used": usage["grams_used"], "hours_used": usage["hours_used"], "jobs_used": usage["jobs_used"]},
        })
    return result


@router.put("/admin/quotas/{user_id}", tags=["Quotas"])
async def admin_set_quota(user_id: int, body: QuotaUpdateRequest, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: set quotas for a user."""
    require_feature("print_quotas")
    user = db.execute(text("SELECT id, group_id FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Org-scoped admin: verify target user is in their group
    if not _is_superadmin(current_user):
        if not _check_org_admin_access(current_user, user.group_id):
            raise HTTPException(status_code=403, detail="Cannot set quotas for users outside your group")

    body_data = body.model_dump(exclude_unset=True)
    sets = []
    params = {"id": user_id}
    for field in ["quota_grams", "quota_hours", "quota_jobs", "quota_period"]:
        if field in body_data:
            sets.append(f"{field} = :{field}")
            params[field] = body_data[field]

    if sets:
        db.execute(text(f"UPDATE users SET {', '.join(sets)} WHERE id = :id"), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.flush()

    log_audit(db, "quota_updated", "user", user_id, f"Quotas updated: {body_data}")
    db.commit()
    return {"status": "ok"}


# ============== GDPR Data Export & Erasure ==============

@router.get("/users/{user_id}/export", tags=["GDPR"], dependencies=[Depends(require_interactive_session())])
async def export_user_data(user_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Export all personal data for a user (GDPR Article 20)."""
    if current_user["id"] != user_id and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Can only export your own data")

    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Org-scoped admin exporting another user: verify they're in the same group
    if current_user["id"] != user_id and not _is_superadmin(current_user):
        if not _check_org_admin_access(current_user, dict(user._mapping).get("group_id")):
            raise HTTPException(status_code=403, detail="Cannot export data for users outside your group")

    u = dict(user._mapping)
    u.pop("password_hash", None)
    u.pop("mfa_secret", None)

    jobs = [dict(r._mapping) for r in db.execute(text("SELECT * FROM jobs WHERE submitted_by = :uid"), {"uid": user_id}).fetchall()]
    # Queue jobs created by operators need no approval and have no submitted_by.
    # They still belong in the personal export through their charge attribution.
    charged_jobs = [dict(r._mapping) for r in db.execute(
        text("SELECT * FROM jobs WHERE charged_to_user_id = :uid"), {"uid": user_id}).fetchall()]
    audit = [dict(r._mapping) for r in db.execute(
        text("SELECT * FROM audit_logs WHERE entity_type = 'user' AND entity_id = :uid ORDER BY timestamp DESC LIMIT 1000"),
        {"uid": user_id}).fetchall()]
    # Actions the user performed (any entity), not only entries about the account.
    actions = [dict(r._mapping) for r in db.execute(
        text("SELECT id, timestamp, action, entity_type, entity_id, details, ip_address FROM audit_logs "
             "WHERE actor_user_id = :uid AND (entity_type IS NULL OR entity_type != 'user' "
             "OR entity_id IS NULL OR entity_id != :uid) "
             "ORDER BY timestamp DESC LIMIT 1000"),
        {"uid": user_id}).fetchall()]
    if not _is_superadmin(current_user):
        for row in audit:
            for field in ("actor_user_id", "actor_username", "actor_auth_kind"):
                row.pop(field, None)
    sessions_data = [dict(r._mapping) for r in db.execute(
        text("SELECT id, ip_address, user_agent, created_at, last_seen_at FROM active_sessions WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
    prefs = [dict(r._mapping) for r in db.execute(text("SELECT * FROM alert_preferences WHERE user_id = :uid"), {"uid": user_id}).fetchall()]
    api_tokens_data = [dict(r._mapping) for r in db.execute(
        text("SELECT id, name, scopes, created_at, last_used_at, expires_at FROM api_tokens WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
    quota_data = [dict(r._mapping) for r in db.execute(
        text("SELECT period_key, grams_used, hours_used, jobs_used, updated_at FROM quota_usage WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
    def _rows(statement):
        return [dict(r._mapping) for r in db.execute(text(statement), {"uid": user_id}).fetchall()]

    alerts_data = _rows("SELECT id, alert_type, severity, title, message, is_read, is_dismissed, printer_id, job_id, "
                        "spool_id, created_at FROM alerts WHERE user_id = :uid ORDER BY created_at DESC")
    push_subscriptions_data = _rows("SELECT id, endpoint, created_at FROM push_subscriptions WHERE user_id = :uid")
    push_devices_data = _rows("SELECT id, device_id, platform, token, live_activity_token, preferences_json, "
                              "created_at, last_seen_at FROM push_devices WHERE user_id = :uid")
    archives_data = _rows("SELECT id, job_id, printer_id, print_name, status, started_at, completed_at, "
                          "actual_duration_seconds, filament_used_grams, cost_estimate, notes, tags, created_at "
                          "FROM print_archives WHERE user_id = :uid ORDER BY created_at DESC")
    uploads_data = _rows("SELECT id, filename, original_filename, project_name, total_weight_grams, printer_model, "
                         "storage_bytes, uploaded_at FROM print_files WHERE created_by = :uid ORDER BY uploaded_at DESC")
    projects_data = _rows("SELECT id, name, description, status, created_at, updated_at FROM projects "
                          "WHERE created_by = :uid ORDER BY created_at DESC")
    classroom_identity = [dict(r._mapping) for r in db.execute(
        text(
            "SELECT org_id, provider_user_id, normalized_email, state, last_seen_at, created_at "
            "FROM classroom_roster_identities WHERE user_id=:uid"
        ),
        {"uid": user_id},
    ).fetchall()]
    classroom_memberships = [dict(r._mapping) for r in db.execute(
        text(
            "SELECT m.provider_course_id, m.course_name, c.display_name AS cost_center_name, "
            "g.role, g.state, g.granted_at, g.revoked_at "
            "FROM education_cost_center_grants g "
            "JOIN classroom_course_mappings m ON m.cost_center_id=g.cost_center_id AND m.org_id=g.org_id "
            "JOIN education_cost_centers c ON c.id=g.cost_center_id "
            "WHERE g.user_id=:uid ORDER BY m.provider_course_id, g.role"
        ),
        {"uid": user_id},
    ).fetchall()]

    from modules.organizations.education_training_policy import export_training_data
    from modules.organizations.education_roster_privacy import export_roster
    from modules.organizations.education_classes_privacy import export_classes
    from modules.organizations.education_program_records import export_program_records
    from core.traceability_privacy import export_personal
    from modules.organizations.education_term_privacy import export_term_data
    export = {
        "term_retention": export_term_data(db, user_id),
        "traceability": export_personal(db, user_id, current_user),
        "training": export_training_data(db, user_id),
        "education_program_reports": export_program_records(db, user_id),
        "education_roster": export_roster(db, user_id),
        "education_classes": export_classes(db, user_id),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": u,
        "jobs_submitted": jobs,
        "jobs_charged": charged_jobs,
        "audit_log_entries": audit,
        "audit_actions_performed": actions,
        "alerts": alerts_data,
        "push_subscriptions": push_subscriptions_data,
        "push_devices": push_devices_data,
        "print_archives": archives_data,
        "uploaded_print_files": uploads_data,
        "projects": projects_data,
        "active_sessions": sessions_data,
        "alert_preferences": prefs,
        "api_tokens": api_tokens_data,
        "quota_usage": quota_data,
        "controlled_part_changes": _rows("SELECT 'file' AS resource_type,file_id AS resource_id,reason FROM controlled_files WHERE changed_by=:uid UNION ALL SELECT 'job' AS resource_type,job_id AS resource_id,reason FROM controlled_jobs WHERE changed_by=:uid"),
        "classroom_identity": classroom_identity,
        "classroom_memberships": classroom_memberships,
    }

    from core.controlled_redaction import redact_export
    export = redact_export(db, current_user, export)
    export["controlled_audience_memberships"] = _rows("SELECT audience_id FROM controlled_audience_members WHERE user_id=:uid")
    log_audit(db, "gdpr_export", "user", user_id, f"Data exported for user {user.username}")
    db.commit()
    return export


@router.delete("/users/{user_id}/erase", tags=["GDPR"], dependencies=[Depends(require_interactive_session())])
async def erase_user_data(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    from starlette.concurrency import run_in_threadpool
    from modules.organizations.gdpr_erasure import erase_user_data_sync
    return await run_in_threadpool(erase_user_data_sync, user_id, current_user, db)
