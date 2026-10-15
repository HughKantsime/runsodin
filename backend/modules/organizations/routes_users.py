"""Organizations users routes — users CRUD, groups, and CSV import."""

import csv
import io
from html import escape
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import execute_insert_returning_id, sql
from core.dependencies import log_audit
from core.rbac import require_scope, require_role, require_superadmin, require_interactive_session, is_interactive_session, CREDENTIAL_ROUTE_DETAIL
from modules.organizations.education_policy import (
    assert_org_hard_delete_allowed,
    assert_user_hard_delete_allowed,
    assert_user_tenant_change_allowed,
)
from core.auth_helpers import _validate_password
from core.auth import ACCESS_TOKEN_EXPIRE_HOURS, hash_password, UserCreate, UserRole
from core.models import SystemConfig
from license_manager import require_feature, check_user_limit

log = logging.getLogger("odin.api")
router = APIRouter()


def _is_superadmin(current_user: dict) -> bool:
    """Superadmin = role admin with no group_id."""
    return current_user.get("role") == "admin" and not current_user.get("group_id")


def _check_org_admin_access(current_user: dict, target_group_id, db=None) -> bool:
    """Check if an org-scoped admin can manage a user with the given group_id.

    Superadmins can manage anyone. Org-scoped admins can only manage users
    in their own group (or unassigned users visible to all).
    """
    if _is_superadmin(current_user):
        return True
    return target_group_id == current_user.get("group_id")


# ============== Email helper ==============

def _send_odin_email(db, to_email: str, subject: str, html_body: str):
    """Send an email using the configured SMTP settings. Returns True on success."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config:
        return False
    smtp = config.value
    if not smtp.get("enabled") or not smtp.get("host"):
        return False

    password = smtp.get("password", "")
    if password:
        try:
            import core.crypto as crypto
            password = crypto.decrypt(password)
        except Exception as e:
            log.debug("SMTP credential decryption unavailable; using existing compatibility value")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp.get("from_address", smtp.get("username", "odin@localhost"))
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    server = None
    accepted = False
    try:
        from core.itar import enforce_host_destination
        enforce_host_destination(smtp["host"], scheme="smtp")
        server = smtplib.SMTP(smtp["host"], smtp.get("port", 587 if smtp.get("use_tls", True) else 25), timeout=10)
        if smtp.get("use_tls", True):
            server.starttls()
        if smtp.get("username") and password:
            server.login(smtp["username"], password)
        refused = server.send_message(msg)
        accepted = type(refused) is dict and not refused
        server.quit()
    except Exception:
        accepted = False
        log.warning("SMTP delivery could not be confirmed")
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                accepted = False
                log.warning("SMTP cleanup could not be confirmed")
    return accepted


# ============== Users CRUD ==============

@router.get("/users", tags=["Users"])
async def list_users(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if _is_superadmin(current_user):
        users = db.execute(text("SELECT id, username, email, role, is_active, last_login, created_at, group_id, mfa_enabled FROM users")).fetchall()
    else:
        # Org-scoped admin: only users in their group (plus unassigned users)
        users = db.execute(text(
            "SELECT id, username, email, role, is_active, last_login, created_at, group_id, mfa_enabled FROM users "
            "WHERE group_id = :gid OR group_id IS NULL"),
            {"gid": current_user["group_id"]}).fetchall()
    return [{**dict(u._mapping), "mfa_enabled": bool(u.mfa_enabled)} for u in users]


@router.post("/users", tags=["Users"], dependencies=[Depends(require_interactive_session())])
async def create_user(user: UserCreate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    # Org-scoped admin: enforce group assignment
    if not _is_superadmin(current_user):
        admin_gid = current_user["group_id"]
        if user.group_id is not None and user.group_id != admin_gid:
            raise HTTPException(status_code=403, detail="Cannot create users in a different group")
        user.group_id = admin_gid  # default to admin's group

    current_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    check_user_limit(current_count)

    import secrets as _secrets
    password = user.password
    if user.send_welcome_email and user.email:
        # Keep 192 random bits while guaranteeing the existing password policy.
        password = _secrets.token_urlsafe(24) + "aA1"

    if not password:
        raise HTTPException(status_code=400, detail="Password is required")
    pw_valid, pw_msg = _validate_password(password)
    if not pw_valid:
        raise HTTPException(status_code=400, detail=pw_msg)

    password_hash = hash_password(password)
    def rollback():
        try:db.rollback()
        except Exception:pass
    try:
        db.execute(text("""
            INSERT INTO users (username, email, password_hash, role, group_id)
            VALUES (:username, :email, :password_hash, :role, :group_id)
        """), {"username": user.username, "email": user.email, "password_hash": password_hash,
               "role": user.role, "group_id": user.group_id})
        db.flush()
    except IntegrityError:
        rollback()
        raise HTTPException(status_code=400, detail="Username or email already exists") from None
    except Exception:
        rollback()
        raise HTTPException(status_code=503, detail="Account creation could not be confirmed. Check the user list before retrying.") from None

    try:
        new_user = db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": user.username}).fetchone()
        if new_user is None:
            raise RuntimeError('Created account identity unavailable')
        log_audit(db, "user.created", "user", new_user.id, {"username": user.username, "role": user.role})
        db.commit()
    except Exception:
        # A commit exception may occur after persistence. Never send or retry
        # from an unconfirmed account transaction, and preserve visible truth.
        rollback()
        raise HTTPException(status_code=503, detail="Account creation could not be confirmed. Check the user list before retrying.") from None

    if user.send_welcome_email and user.email:
        html = f"""
        <html><body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
        <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
            <h2 style="color: #3b82f6; margin-top: 0;">Welcome to O.D.I.N.</h2>
            <p>Your account has been created. Here are your login details:</p>
            <p><strong>Username:</strong> {escape(user.username)}<br>
               <strong>Temporary Password:</strong> {escape(password)}</p>
            <p>Please change your password after your first login.</p>
            <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
            <p style="color: #888; font-size: 12px;">O.D.I.N. — Orchestrated Dispatch &amp; Inventory Network</p>
        </div></body></html>
        """
        try:
            sent = _send_odin_email(db, user.email, "Your O.D.I.N. Account", html) is True
        except Exception:
            sent = False
        if not sent:
            return {"status": "created", "welcome_email": "unconfirmed",
                    "message": "Account created, but welcome email delivery was not confirmed. Check email settings, then use Send password reset link for this user."}
        return {"status": "created", "welcome_email": "sent"}
    return {"status": "created"}


@router.post("/users/{user_id}/reset-password-email", tags=["Users"], dependencies=[Depends(require_interactive_session())])
async def reset_password_email(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin action: email a reset link without changing the user's password."""
    from modules.organizations.password_reset import reset_mail_base_url, send_reset_link

    user_row = db.execute(text("SELECT username, email, group_id, is_active FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not _is_superadmin(current_user) and not _check_org_admin_access(current_user, user_row.group_id):
        raise HTTPException(status_code=403, detail="Cannot reset password for users outside your group")
    if not user_row.is_active:
        raise HTTPException(status_code=400, detail="User account is disabled")
    if not user_row.email:
        raise HTTPException(status_code=400, detail="User has no email address")
    base_url = reset_mail_base_url(db)
    accepted = send_reset_link(db, user_id, user_row.username, user_row.email, _send_odin_email,
                              base_url=base_url, actor_user_id=current_user["id"])
    if not accepted:
        raise HTTPException(status_code=503, detail="Password reset email could not be sent. Your password has not changed.")
    return {"status": "password_reset_email_sent"}


class UserUpdateRequest(PydanticBaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None
    group_id: Optional[int] = None


@router.patch("/users/{user_id}", tags=["Users"], dependencies=[Depends(require_scope("admin"))])
async def update_user(user_id: int, body: UserUpdateRequest, request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    updates = body.model_dump(exclude_unset=True)
    target = db.execute(
        text("SELECT group_id FROM users WHERE id = :id"), {"id": user_id}
    ).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Org-scoped admin: verify target user is in their group
    if not _is_superadmin(current_user):
        if not _check_org_admin_access(current_user, target.group_id):
            raise HTTPException(status_code=403, detail="Cannot modify users outside your group")
        # Prevent org-scoped admin from moving user to a different group
        if "group_id" in updates and updates["group_id"] != current_user["group_id"]:
            raise HTTPException(status_code=403, detail="Cannot move users to a different group")
    if "group_id" in updates and updates["group_id"] != target.group_id:
        assert_user_tenant_change_allowed(db, user_id)

    # Email and username are account-recovery material (a forgot-password link
    # goes to the stored email; the username names the account), so CHANGING
    # either is credential material and needs an interactive session. Setting a
    # field to its current value (or resending it unchanged) is a no-op and is
    # allowed; clearing it to null is a change.
    current_recovery = db.execute(
        text("SELECT email, username FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    recovery_change = (
        ("email" in updates and updates["email"] != current_recovery.email)
        or ("username" in updates and updates["username"] != current_recovery.username))
    if (recovery_change or ('password' in updates and updates['password'])) and not is_interactive_session(current_user):
        raise HTTPException(status_code=403, detail=CREDENTIAL_ROUTE_DETAIL)

    if 'password' in updates and updates['password']:
        pw_valid, pw_msg = _validate_password(updates['password'])
        if not pw_valid:
            raise HTTPException(status_code=400, detail=pw_msg)
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)

    ALLOWED_USER_FIELDS = {"username", "email", "role", "is_active", "password_hash", "group_id"}
    updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}

    email_changed = 'email' in updates and updates['email'] != current_recovery.email
    password_changed = 'password_hash' in updates
    role_changed = 'role' in updates
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates['id'] = user_id
        db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.flush()
    if role_changed:
        log_audit(db, "user.role_changed", "user", user_id,
                  {"new_role": updates.get("role"), "actor_user_id": current_user["id"]})
    if email_changed:
        # The recovery address moved: void outstanding reset links and the
        # 30-day biometric tokens bound to the old identity.
        from modules.organizations.password_reset import invalidate_recovery_credentials
        invalidate_recovery_credentials(db, user_id)
    if password_changed:
        from core.mfa_login import cancel_pending_enrollments
        from modules.organizations.password_reset import revoke_sessions_and_biometric, current_session_jti
        cancel_pending_enrollments(db, user_id)
        log_audit(db, "user.password_changed", "user", user_id,
                  {"actor_user_id": current_user["id"], "target_user_id": user_id})
        # Revoke biometric tokens and every other session. On an admin changing
        # their OWN password, keep the current session alive.
        keep_jti = current_session_jti(request) if current_user["id"] == user_id else None
        revoke_sessions_and_biometric(db, user_id, keep_jti=keep_jti)
    db.commit()
    return {"status": "updated"}


@router.delete("/users/{user_id}", tags=["Users"], dependencies=[Depends(require_scope("admin"))])
async def delete_user(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    # Lock the account first so a concurrent login cannot add a session mid-delete.
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("UPDATE users SET id = id WHERE id = :id"), {"id": user_id})
    target = db.execute(text("SELECT role, group_id FROM users WHERE id = :id" +
                             (" FOR UPDATE" if db.get_bind().dialect.name == "postgresql" else "")),
                        {"id": user_id}).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Org-scoped admin: verify target is in their group
    if not _is_superadmin(current_user):
        if not _check_org_admin_access(current_user, target.group_id):
            raise HTTPException(status_code=403, detail="Cannot delete users outside your group")
    if target.role == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active IS TRUE")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin account")
    assert_user_hard_delete_allowed(db, user_id)
    retained = [label for label, sql in _USER_HISTORY_REFERENCES
                if db.execute(text(sql), {"uid": user_id}).first() is not None]
    if retained:
        raise HTTPException(
            status_code=409,
            detail="User has retained history (" + ", ".join(retained) + "). "
                   "Deactivate the account or use GDPR erase to anonymize it instead.",
        )
    target_row = db.execute(text("SELECT username FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    try:
        _revoke_user_credentials(db, user_id)
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        log_audit(db, "user.deleted", "user", user_id,
                  {"deleted_username": target_row.username if target_row else str(user_id),
                   "actor_user_id": current_user["id"]})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"status": "deleted"}


# History that must keep its owner; hard delete is refused while any exists.
_USER_HISTORY_REFERENCES = (
    ("jobs", "SELECT 1 FROM jobs WHERE submitted_by = :uid OR charged_to_user_id = :uid OR approved_by = :uid LIMIT 1"),
    ("vision reviews", "SELECT 1 FROM vision_detections WHERE reviewed_by = :uid LIMIT 1"),
    ("print archives", "SELECT 1 FROM print_archives WHERE user_id = :uid LIMIT 1"),
    ("projects", "SELECT 1 FROM projects WHERE created_by = :uid LIMIT 1"),
    ("print files", "SELECT 1 FROM print_files WHERE created_by = :uid LIMIT 1"),
    ("model revisions", "SELECT 1 FROM model_revisions WHERE uploaded_by = :uid LIMIT 1"),
    ("owned groups", "SELECT 1 FROM groups WHERE owner_id = :uid LIMIT 1"),
    ("report schedules", "SELECT 1 FROM report_schedules WHERE created_by = :uid LIMIT 1"),
    ("printer profiles", "SELECT 1 FROM printer_profiles WHERE created_by = :uid LIMIT 1"),
    ("printer profile revisions", "SELECT 1 FROM printer_profile_revisions WHERE created_by = :uid LIMIT 1"),
    ("classroom connections", "SELECT 1 FROM classroom_connections WHERE created_by = :uid LIMIT 1"),
)

# Credentials and per-user state removed with the account (caller owns the transaction).
_USER_CREDENTIAL_TABLES = (
    ("api_tokens", "user_id"), ("mfa_enrollments", "user_id"),
    ("password_reset_tokens", "user_id"), ("push_subscriptions", "user_id"),
    ("push_devices", "user_id"), ("biometric_tokens", "user_id"),
    ("alert_preferences", "user_id"), ("alerts", "user_id"),
    ("idempotency_keys", "user_id"), ("quota_usage", "user_id"),
    ("quiet_hours_digest_sends", "user_id"), ("classroom_roster_identities", "user_id"),
    ("classroom_oauth_states", "admin_id"), ("active_sessions", "user_id"),
)


def _revoke_user_credentials(db, user_id):
    """Blacklist every recorded session jti, then delete all credential rows."""
    expires = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    jtis = [row.token_jti for row in db.execute(
        text("SELECT token_jti FROM active_sessions WHERE user_id = :uid AND token_jti IS NOT NULL"),
        {"uid": user_id})]
    for jti in jtis:
        db.execute(text(f"{sql.insert_or_ignore_prefix()} token_blacklist (jti, expires_at) "
                        f"VALUES (:jti, :expires){sql.on_conflict_ignore('jti')}"),
                   {"jti": jti, "expires": expires})
    for table, column in _USER_CREDENTIAL_TABLES:
        db.execute(text(f"DELETE FROM {table} WHERE {column} = :uid"), {"uid": user_id})


@router.post("/users/import", tags=["Users"], dependencies=[Depends(require_interactive_session())])
async def import_users_csv(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Bulk import users from a CSV file. Admin only."""
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text_content))

    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is empty or has no header row")
    lower_fields = [f.strip().lower() for f in reader.fieldnames]
    if "username" not in lower_fields or "email" not in lower_fields or "password" not in lower_fields:
        raise HTTPException(status_code=400, detail="CSV must have columns: username, email, password (and optionally role)")

    valid_roles = {"admin", "operator", "viewer"}
    email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    existing = {r[0] for r in db.execute(text("SELECT username FROM users")).fetchall()}
    current_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()

    created = 0
    skipped = 0
    errors = []

    for row_num, raw_row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v.strip() if v else "") for k, v in raw_row.items()}

        username = row.get("username", "")
        email = row.get("email", "")
        password = row.get("password", "")
        role = row.get("role", "").lower() or "viewer"

        if not username:
            errors.append({"row": row_num, "reason": "Missing username"})
            continue
        if not email:
            errors.append({"row": row_num, "reason": "Missing email"})
            continue
        if not email_re.match(email):
            errors.append({"row": row_num, "reason": f"Invalid email format: {email}"})
            continue
        if not password:
            errors.append({"row": row_num, "reason": "Missing password"})
            continue

        pw_valid, pw_msg = _validate_password(password)
        if not pw_valid:
            errors.append({"row": row_num, "reason": pw_msg})
            continue

        if role not in valid_roles:
            errors.append({"row": row_num, "reason": f"Invalid role '{role}'. Must be admin, operator, or viewer"})
            continue

        if username in existing:
            skipped += 1
            continue

        try:
            check_user_limit(current_count)
        except HTTPException:
            errors.append({"row": row_num, "reason": "License user limit reached"})
            break

        # Org-scoped admin: assign imported users to their group
        import_group_id = current_user.get("group_id") if not _is_superadmin(current_user) else None

        password_hash_val = hash_password(password)
        try:
            db.execute(text("INSERT INTO users (username, email, password_hash, role, group_id) VALUES (:username, :email, :password_hash, :role, :group_id)"),
                       {"username": username, "email": email, "password_hash": password_hash_val, "role": role, "group_id": import_group_id})
            db.commit()
            existing.add(username)
            current_count += 1
            created += 1
        except Exception:
            db.rollback()
            skipped += 1

    log_audit(db, "users_imported", "user", details=f"CSV import: {created} created, {skipped} skipped, {len(errors)} errors")
    db.commit()
    return {"created": created, "skipped": skipped, "errors": errors}


# ============== Groups ==============

@router.get("/groups", tags=["Groups"])
async def list_groups(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    if current_user.get("role") == "admin":
        groups = db.execute(text("""
            SELECT g.id, g.name, g.description, g.owner_id, g.created_at, g.updated_at,
                   u.username AS owner_username,
                   (SELECT COUNT(*) FROM users WHERE group_id = g.id) AS member_count
            FROM groups g LEFT JOIN users u ON u.id = g.owner_id ORDER BY g.name
        """)).fetchall()
    else:
        user_group_id = current_user.get("group_id")
        if user_group_id is None:
            return []
        groups = db.execute(text("""
            SELECT g.id, g.name, g.description, g.owner_id, g.created_at, g.updated_at,
                   u.username AS owner_username,
                   (SELECT COUNT(*) FROM users WHERE group_id = g.id) AS member_count
            FROM groups g LEFT JOIN users u ON u.id = g.owner_id
            WHERE g.id = :gid ORDER BY g.name
        """), {"gid": user_group_id}).fetchall()
    return [dict(g._mapping) for g in groups]


@router.post("/groups", tags=["Groups"])
async def create_group(body: dict, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    require_feature("user_groups")
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    description = body.get("description", "").strip() or None
    owner_id = body.get("owner_id")

    if owner_id:
        owner = db.execute(text("SELECT role FROM users WHERE id = :id AND is_active IS TRUE"), {"id": owner_id}).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner not found")
        if owner.role not in ("operator", "admin"):
            raise HTTPException(status_code=400, detail="Group owner must be an operator or admin")

    try:
        group_id = execute_insert_returning_id(
            db,
            "INSERT INTO groups (name, description, owner_id) "
            "VALUES (:name, :description, :owner_id)",
            {"name": name, "description": description, "owner_id": owner_id},
        )
        db.commit()
        return {"status": "created", "id": group_id}
    except Exception:
        raise HTTPException(status_code=400, detail="Group name already exists")


@router.get("/groups/{group_id}", tags=["Groups"])
async def get_group(group_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    if current_user.get("role") != "admin" and current_user.get("group_id") != group_id:
        raise HTTPException(status_code=403, detail="Access denied")
    group = db.execute(text("""
        SELECT g.id, g.name, g.description, g.owner_id, g.created_at, g.updated_at,
               u.username AS owner_username
        FROM groups g LEFT JOIN users u ON u.id = g.owner_id WHERE g.id = :id
    """), {"id": group_id}).fetchone()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    members = db.execute(text("SELECT id, username, email, role FROM users WHERE group_id = :gid"), {"gid": group_id}).fetchall()
    result = dict(group._mapping)
    result["members"] = [dict(m._mapping) for m in members]
    return result


@router.patch("/groups/{group_id}", tags=["Groups"])
async def update_group(group_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    if not _is_superadmin(current_user) and current_user.get("group_id") != group_id:
        raise HTTPException(status_code=403, detail="Can only manage your own group")
    existing = db.execute(text("SELECT id FROM groups WHERE id = :id"), {"id": group_id}).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")
    ALLOWED_GROUP_FIELDS = {"name", "description", "owner_id"}
    updates = {k: v for k, v in body.items() if k in ALLOWED_GROUP_FIELDS}

    if "owner_id" in updates and updates["owner_id"]:
        owner = db.execute(text("SELECT role FROM users WHERE id = :id AND is_active IS TRUE"), {"id": updates["owner_id"]}).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner not found")
        if owner.role not in ("operator", "admin"):
            raise HTTPException(status_code=400, detail="Group owner must be an operator or admin")

    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates["id"] = group_id
        db.execute(text(f"UPDATE groups SET {set_clause} WHERE id = :id"), updates)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()
    return {"status": "updated"}


@router.delete("/groups/{group_id}", tags=["Groups"])
async def delete_group(group_id: int, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    require_feature("user_groups")
    existing = db.execute(text("SELECT id FROM groups WHERE id = :id"), {"id": group_id}).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")
    assert_org_hard_delete_allowed(db, group_id)
    db.execute(text("UPDATE users SET group_id = NULL WHERE group_id = :gid"), {"gid": group_id})
    db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": group_id})
    db.commit()
    return {"status": "deleted"}
