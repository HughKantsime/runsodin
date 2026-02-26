"""Organizations users routes — users CRUD, groups, and CSV import."""

import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role
from core.auth_helpers import _validate_password
from core.auth import hash_password, UserCreate
from core.models import SystemConfig
from license_manager import require_feature, check_user_limit

log = logging.getLogger("odin.api")
router = APIRouter()


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
        except Exception:
            pass

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp.get("from_address", smtp.get("username", "odin@localhost"))
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        if smtp.get("use_tls", True):
            server = smtplib.SMTP(smtp["host"], smtp.get("port", 587), timeout=10)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp["host"], smtp.get("port", 25), timeout=10)
        if smtp.get("username") and password:
            server.login(smtp["username"], password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        log.error(f"SMTP send failed: {e}")
        return False


# ============== Users CRUD ==============

@router.get("/users", tags=["Users"])
async def list_users(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    users = db.execute(text("SELECT id, username, email, role, is_active, last_login, created_at, group_id FROM users")).fetchall()
    return [dict(u._mapping) for u in users]


@router.post("/users", tags=["Users"])
async def create_user(user: UserCreate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    current_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    check_user_limit(current_count)

    import secrets as _secrets
    password = user.password
    if user.send_welcome_email and user.email:
        password = _secrets.token_urlsafe(16)

    if not password:
        raise HTTPException(status_code=400, detail="Password is required")

    password_hash = hash_password(password)
    try:
        db.execute(text("""
            INSERT INTO users (username, email, password_hash, role, group_id)
            VALUES (:username, :email, :password_hash, :role, :group_id)
        """), {"username": user.username, "email": user.email, "password_hash": password_hash,
               "role": user.role, "group_id": user.group_id})
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Username or email already exists")

    if user.send_welcome_email and user.email:
        html = f"""
        <html><body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
        <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
            <h2 style="color: #3b82f6; margin-top: 0;">Welcome to O.D.I.N.</h2>
            <p>Your account has been created. Here are your login details:</p>
            <p><strong>Username:</strong> {user.username}<br>
               <strong>Temporary Password:</strong> {password}</p>
            <p>Please change your password after your first login.</p>
            <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
            <p style="color: #888; font-size: 12px;">O.D.I.N. — Orchestrated Dispatch &amp; Inventory Network</p>
        </div></body></html>
        """
        _send_odin_email(db, user.email, "Your O.D.I.N. Account", html)

    return {"status": "created"}


@router.post("/users/{user_id}/reset-password-email", tags=["Users"])
async def reset_password_email(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Admin action: generate a new random password and email it to the user."""
    import secrets as _secrets

    user_row = db.execute(text("SELECT username, email FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_row[1]:
        raise HTTPException(status_code=400, detail="User has no email address")

    new_password = _secrets.token_urlsafe(16)
    password_hash = hash_password(new_password)
    db.execute(text("UPDATE users SET password_hash = :h WHERE id = :id"), {"h": password_hash, "id": user_id})
    db.commit()

    html = f"""
    <html><body style="font-family: Arial, sans-serif; padding: 20px; background: #1a1a1a; color: #e0e0e0;">
    <div style="max-width: 500px; margin: 0 auto; background: #2a2a2a; padding: 20px; border-radius: 8px;">
        <h2 style="color: #3b82f6; margin-top: 0;">Password Reset — O.D.I.N.</h2>
        <p>Your password has been reset by an administrator.</p>
        <p><strong>Username:</strong> {user_row[0]}<br>
           <strong>New Password:</strong> {new_password}</p>
        <p>Please change your password after logging in.</p>
        <hr style="border: none; border-top: 1px solid #444; margin: 20px 0;">
        <p style="color: #888; font-size: 12px;">O.D.I.N.</p>
    </div></body></html>
    """
    sent = _send_odin_email(db, user_row[1], "O.D.I.N. Password Reset", html)
    if not sent:
        raise HTTPException(status_code=503, detail="SMTP is not configured or send failed")

    log_audit(db, "user.password_reset_email", "user", user_id,
              {"actor_user_id": current_user["id"], "target_user_id": user_id})
    return {"status": "password_reset_email_sent"}


@router.patch("/users/{user_id}", tags=["Users"])
async def update_user(user_id: int, updates: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if 'password' in updates and updates['password']:
        pw_valid, pw_msg = _validate_password(updates['password'])
        if not pw_valid:
            raise HTTPException(status_code=400, detail=pw_msg)
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)

    ALLOWED_USER_FIELDS = {"username", "email", "role", "is_active", "password_hash", "group_id"}
    updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}

    password_changed = 'password_hash' in updates
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates['id'] = user_id
        db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    if password_changed:
        log_audit(db, "user.password_changed", "user", user_id,
                  {"actor_user_id": current_user["id"], "target_user_id": user_id})
        sessions = db.execute(text("SELECT token_jti FROM active_sessions WHERE user_id = :uid"), {"uid": user_id}).fetchall()
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for session in sessions:
            db.execute(text("INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (:jti, :exp)"),
                       {"jti": session.token_jti, "exp": expiry})
        db.execute(text("DELETE FROM active_sessions WHERE user_id = :uid"), {"uid": user_id})
        db.commit()
    return {"status": "updated"}


@router.delete("/users/{user_id}", tags=["Users"])
async def delete_user(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = db.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user_id}).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="Cannot delete the last admin account")
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()
    return {"status": "deleted"}


@router.post("/users/import", tags=["Users"])
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

        password_hash_val = hash_password(password)
        try:
            db.execute(text("INSERT INTO users (username, email, password_hash, role) VALUES (:username, :email, :password_hash, :role)"),
                       {"username": username, "email": email, "password_hash": password_hash_val, "role": role})
            db.commit()
            existing.add(username)
            current_count += 1
            created += 1
        except Exception:
            db.rollback()
            skipped += 1

    log_audit(db, "users_imported", "user", details=f"CSV import: {created} created, {skipped} skipped, {len(errors)} errors")
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
async def create_group(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    description = body.get("description", "").strip() or None
    owner_id = body.get("owner_id")

    if owner_id:
        owner = db.execute(text("SELECT role FROM users WHERE id = :id AND is_active = 1"), {"id": owner_id}).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner not found")
        if owner.role not in ("operator", "admin"):
            raise HTTPException(status_code=400, detail="Group owner must be an operator or admin")

    try:
        result = db.execute(text("INSERT INTO groups (name, description, owner_id) VALUES (:name, :description, :owner_id)"),
                            {"name": name, "description": description, "owner_id": owner_id})
        db.commit()
        return {"status": "created", "id": result.lastrowid}
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
    existing = db.execute(text("SELECT id FROM groups WHERE id = :id"), {"id": group_id}).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")

    ALLOWED_GROUP_FIELDS = {"name", "description", "owner_id"}
    updates = {k: v for k, v in body.items() if k in ALLOWED_GROUP_FIELDS}

    if "owner_id" in updates and updates["owner_id"]:
        owner = db.execute(text("SELECT role FROM users WHERE id = :id AND is_active = 1"), {"id": updates["owner_id"]}).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Owner not found")
        if owner.role not in ("operator", "admin"):
            raise HTTPException(status_code=400, detail="Group owner must be an operator or admin")

    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates["id"] = group_id
        db.execute(text(f"UPDATE groups SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    return {"status": "updated"}


@router.delete("/groups/{group_id}", tags=["Groups"])
async def delete_group(group_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    require_feature("user_groups")
    existing = db.execute(text("SELECT id FROM groups WHERE id = :id"), {"id": group_id}).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")
    db.execute(text("UPDATE users SET group_id = NULL WHERE group_id = :gid"), {"gid": group_id})
    db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": group_id})
    db.commit()
    return {"status": "deleted"}
