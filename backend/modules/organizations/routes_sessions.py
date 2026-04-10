"""Organizations sessions routes — session management, API tokens, quotas, and GDPR."""

import json
import logging
from datetime import datetime, timedelta, timezone

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role
from core.auth import hash_password
import core.auth as auth_module
from core.quota import _get_quota_usage
from core.rate_limit import limiter

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


@router.delete("/sessions/{session_id}", tags=["Sessions"])
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


@router.delete("/sessions", tags=["Sessions"])
async def revoke_all_sessions(request: Request, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke all sessions except the current one."""
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


@router.delete("/admin/sessions/{session_id}", tags=["Sessions"])
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
    db.commit()
    log_audit(db, "session_revoked_admin", "session", session_id, f"Admin revoked session for user_id={row.user_id}")
    return {"status": "ok"}


# ============== Scoped API Tokens ==============

VALID_SCOPES = {
    "read", "write",
    "read:printers", "write:printers",
    "read:jobs", "write:jobs",
    "read:spools", "write:spools",
    "read:models", "write:models",
    "read:analytics",
    "admin",
}


@router.post("/tokens", tags=["API Tokens"])
@limiter.limit("10/minute")
async def create_api_token(request: Request, body: dict, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Create a new scoped API token for the current user."""
    import secrets
    name = body.get("name", "").strip()
    scopes = body.get("scopes", [])
    expires_days = body.get("expires_days")

    if not name:
        raise HTTPException(status_code=400, detail="Token name is required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Token name too long")
    if not isinstance(scopes, list):
        raise HTTPException(status_code=400, detail="Scopes must be a list")
    invalid = set(scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid scopes: {', '.join(invalid)}")
    if "admin" in scopes and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create tokens with admin scope")

    raw_token = f"odin_{secrets.token_urlsafe(32)}"
    token_prefix = raw_token[:10]
    token_hash_val = hash_password(raw_token)

    expires_at = None
    if expires_days and int(expires_days) > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(expires_days))

    insert_sql = """INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, scopes, expires_at)
                       VALUES (:user_id, :name, :token_hash, :prefix, :scopes, :expires_at)"""
    params = {"user_id": current_user["id"], "name": name, "token_hash": token_hash_val,
              "prefix": token_prefix, "scopes": json.dumps(scopes), "expires_at": expires_at}
    if sql.is_sqlite:
        db.execute(text(insert_sql), params)
        db.commit()
        token_id = db.execute(text("SELECT last_insert_rowid()")).scalar()
    else:
        token_id = db.execute(text(insert_sql + " RETURNING id"), params).scalar()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()
    log_audit(db, "api_token_created", "api_token", token_id, f"Token '{name}' created")

    return {
        "id": token_id, "name": name, "token": raw_token, "prefix": token_prefix,
        "scopes": scopes, "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/tokens", tags=["API Tokens"])
async def list_api_tokens(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List all API tokens for the current user."""
    rows = db.execute(text(
        "SELECT id, name, token_prefix, scopes, expires_at, last_used_at, created_at "
        "FROM api_tokens WHERE user_id = :uid ORDER BY created_at DESC"),
        {"uid": current_user["id"]}).fetchall()
    return [{
        "id": r.id, "name": r.name, "prefix": r.token_prefix,
        "scopes": json.loads(r.scopes) if r.scopes else [],
        "expires_at": r.expires_at, "last_used_at": r.last_used_at, "created_at": r.created_at,
    } for r in rows]


@router.delete("/tokens/{token_id}", tags=["API Tokens"])
async def revoke_api_token(token_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Revoke (delete) an API token."""
    row = db.execute(text("SELECT * FROM api_tokens WHERE id = :id AND user_id = :uid"),
                     {"id": token_id, "uid": current_user["id"]}).fetchone()
    if not row:
        if current_user["role"] == "admin":
            row = db.execute(text("SELECT * FROM api_tokens WHERE id = :id"), {"id": token_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")

    db.execute(text("DELETE FROM api_tokens WHERE id = :id"), {"id": token_id})
    db.commit()
    log_audit(db, "api_token_revoked", "api_token", token_id, f"Token '{row.name}' revoked")
    return {"status": "ok"}


# ============== Print Quotas ==============

@router.get("/quotas", tags=["Quotas"])
async def get_my_quota(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get current user's quota config and usage."""
    period = current_user.get("quota_period") or "monthly"
    usage = _get_quota_usage(db, current_user["id"], period)
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
    if _is_superadmin(current_user):
        users = db.execute(text(
            "SELECT id, username, quota_grams, quota_hours, quota_jobs, quota_period FROM users WHERE is_active = 1"
        )).fetchall()
    else:
        users = db.execute(text(
            "SELECT id, username, quota_grams, quota_hours, quota_jobs, quota_period FROM users "
            "WHERE is_active = 1 AND (group_id = :gid OR group_id IS NULL)"),
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


class QuotaUpdateRequest(PydanticBaseModel):
    quota_grams: Optional[float] = None
    quota_hours: Optional[float] = None
    quota_jobs: Optional[int] = None
    quota_period: Optional[str] = None


@router.put("/admin/quotas/{user_id}", tags=["Quotas"])
async def admin_set_quota(user_id: int, body: QuotaUpdateRequest, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin: set quotas for a user."""
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
        db.commit()

    log_audit(db, "quota_updated", "user", user_id, f"Quotas updated: {body_data}")
    return {"status": "ok"}


# ============== GDPR Data Export & Erasure ==============

@router.get("/users/{user_id}/export", tags=["GDPR"])
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
    audit = [dict(r._mapping) for r in db.execute(
        text("SELECT * FROM audit_logs WHERE entity_type = 'user' AND entity_id = :uid ORDER BY timestamp DESC LIMIT 1000"),
        {"uid": user_id}).fetchall()]
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

    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": u,
        "jobs_submitted": jobs,
        "audit_log_entries": audit,
        "active_sessions": sessions_data,
        "alert_preferences": prefs,
        "api_tokens": api_tokens_data,
        "quota_usage": quota_data,
    }

    log_audit(db, "gdpr_export", "user", user_id, f"Data exported for user {user.username}")
    return export


@router.delete("/users/{user_id}/erase", tags=["GDPR"])
async def erase_user_data(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Anonymize user data (GDPR Article 17). Admin only. Preserves job records for analytics."""
    user = db.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Org-scoped admin: verify target is in their group
    if not _is_superadmin(current_user):
        if not _check_org_admin_access(current_user, dict(user._mapping).get("group_id")):
            raise HTTPException(status_code=403, detail="Cannot erase users outside your group")
    if user.role == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot erase the last admin account")

    db.execute(text("""UPDATE users SET
        username = :anon_name, email = '[deleted]', password_hash = '[deleted]',
        is_active = 0, mfa_enabled = 0, mfa_secret = NULL,
        oidc_subject = NULL, oidc_provider = NULL
        WHERE id = :id"""),
        {"anon_name": f"[deleted-{user_id}]", "id": user_id})

    db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM api_tokens WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM alert_preferences WHERE user_id = :uid"), {"uid": user_id})
    db.execute(text("DELETE FROM push_subscriptions WHERE user_id = :uid"), {"uid": user_id})
    db.commit()

    log_audit(db, "gdpr_erasure", "user", user_id, f"User data erased (was: {user.username})")
    return {"status": "ok", "message": f"User {user.username} data erased"}
