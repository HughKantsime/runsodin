"""O.D.I.N. — Organization Routes"""

# Domain: organizations
# Depends on: core
# Owns tables: groups, oidc_config, oidc_pending_states, oidc_auth_codes, quota_usage

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime
import json
import logging

import core.crypto as crypto
from core.db import get_db
from core.db_compat import sql
from core.rbac import require_role, require_superadmin, get_org_scope
from core.dependencies import log_audit
from core.webhook_utils import _validate_webhook_url

log = logging.getLogger("odin.api")
router = APIRouter()

# Org-level settings defaults and allowed keys
DEFAULT_ORG_SETTINGS = {
    "default_filament_type": None,
    "default_filament_color": None,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    "webhook_url": None,
    "webhook_type": "generic",
    "branding_app_name": None,
    "branding_logo_url": None,
}

ALLOWED_SETTINGS_KEYS = set(DEFAULT_ORG_SETTINGS.keys())


def _get_org_settings(db, org_id: int) -> dict:
    """Load org settings from groups.settings_json, merged with defaults."""
    row = db.execute(text("SELECT settings_json FROM groups WHERE id = :id"), {"id": org_id}).fetchone()
    if row and row.settings_json:
        stored = json.loads(row.settings_json)
        # Decrypt webhook_url if it was stored encrypted (migration-safe fallback)
        if stored.get("webhook_url"):
            try:
                stored["webhook_url"] = crypto.decrypt(stored["webhook_url"])
            except Exception as e:
                log.debug(f"Failed to decrypt webhook_url (using raw): {e}")
        return {**DEFAULT_ORG_SETTINGS, **stored}
    return dict(DEFAULT_ORG_SETTINGS)


# =============================================================================
# Organizations CRUD
# =============================================================================

@router.get("/orgs", tags=["Organizations"])
async def list_orgs(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """List all organizations. Org-scoped admins see only their own org."""
    user_group = current_user.get("group_id")
    if user_group:
        rows = db.execute(text(
            "SELECT g.*, g.settings_json, "
            "(SELECT COUNT(*) FROM users WHERE group_id = g.id) as member_count "
            "FROM groups g WHERE g.is_org = 1 AND g.id = :gid ORDER BY g.name"),
            {"gid": user_group}).fetchall()
        return [{
            "id": r.id, "name": r.name, "description": r.description,
            "owner_id": r.owner_id, "member_count": r.member_count,
            "created_at": r.created_at, "has_settings": bool(r.settings_json),
        } for r in rows]
    rows = db.execute(text(
        "SELECT g.*, g.settings_json, "
        "(SELECT COUNT(*) FROM users WHERE group_id = g.id) as member_count "
        "FROM groups g WHERE g.is_org = 1 ORDER BY g.name")).fetchall()
    return [{
        "id": r.id, "name": r.name, "description": r.description,
        "owner_id": r.owner_id, "member_count": r.member_count,
        "created_at": r.created_at,
        "has_settings": bool(r.settings_json),
    } for r in rows]


@router.post("/orgs", tags=["Organizations"])
async def create_org(body: dict, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Create a new organization. Superadmin only."""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Organization name is required")

    existing = db.execute(text("SELECT 1 FROM groups WHERE name = :name"), {"name": name}).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Organization name already exists")

    insert_sql = """INSERT INTO groups (name, description, owner_id, is_org)
                       VALUES (:name, :desc, :owner, 1)"""
    params = {"name": name, "desc": body.get("description", ""), "owner": current_user["id"]}
    if sql.is_sqlite:
        db.execute(text(insert_sql), params)
        db.commit()
        org_id = db.execute(text("SELECT last_insert_rowid()")).scalar()
    else:
        org_id = db.execute(text(insert_sql + " RETURNING id"), params).scalar()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()

    log_audit(db, "org_created", "org", org_id, f"Organization '{name}' created")
    return {"id": org_id, "name": name, "status": "ok"}


@router.patch("/orgs/{org_id}", tags=["Organizations"])
async def update_org(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update an organization. Org-scoped admins can only update their own org."""
    if current_user.get("group_id") and current_user["group_id"] != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    org = db.execute(text("SELECT * FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    sets = []
    params = {"id": org_id}
    for field in ["name", "description", "owner_id"]:
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]
    if sets:
        db.execute(text(f"UPDATE groups SET {', '.join(sets)} WHERE id = :id"), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()

    return {"status": "ok"}


@router.delete("/orgs/{org_id}", tags=["Organizations"])
async def delete_org(org_id: int, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Delete an organization. Superadmin only."""
    org = db.execute(text("SELECT * FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Unlink members
    db.execute(text("UPDATE users SET group_id = NULL WHERE group_id = :id"), {"id": org_id})
    # Unlink resources (table names from constant allowlist, not user input)
    _ORG_RESOURCE_TABLES = ("printers", "models", "spools")
    for tbl in _ORG_RESOURCE_TABLES:
        db.execute(text(f"UPDATE {tbl} SET org_id = NULL WHERE org_id = :id"), {"id": org_id})  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": org_id})
    db.commit()

    log_audit(db, "org_deleted", "org", org_id, f"Organization '{org.name}' deleted")
    return {"status": "ok"}


@router.post("/orgs/{org_id}/members", tags=["Organizations"])
async def add_org_member(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Add a user to an organization. Org-scoped admins can only add to their own org."""
    if current_user.get("group_id") and current_user["group_id"] != org_id:
        raise HTTPException(status_code=403, detail="Cannot manage other organizations")
    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    org = db.execute(text("SELECT 1 FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    db.execute(text("UPDATE users SET group_id = :org_id WHERE id = :uid"), {"org_id": org_id, "uid": user_id})
    db.commit()
    return {"status": "ok"}


class AssignPrinterRequest(PydanticBaseModel):
    printer_id: int


@router.post("/orgs/{org_id}/printers", tags=["Organizations"])
async def assign_printer_to_org(org_id: int, body: AssignPrinterRequest, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Assign a printer to an organization. Superadmin only."""
    printer_id = body.printer_id
    db.execute(text("UPDATE printers SET org_id = :oid WHERE id = :pid"),
               {"oid": org_id, "pid": printer_id})
    db.commit()
    return {"status": "ok"}


# =============================================================================
# Organization Settings
# =============================================================================

@router.get("/orgs/{org_id}/settings", tags=["Organizations"])
async def get_org_settings(org_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get org-level settings. Org-scoped admins can only view their own org."""
    if current_user.get("group_id") and current_user["group_id"] != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    org = db.execute(text("SELECT 1 FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _get_org_settings(db, org_id)


@router.put("/orgs/{org_id}/settings", tags=["Organizations"])
async def update_org_settings(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update org-level settings. Org-scoped admins can only update their own org."""
    if current_user.get("group_id") and current_user["group_id"] != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    org = db.execute(text("SELECT name, settings_json FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # SSRF validation and encryption for webhook URL before persisting
    if "webhook_url" in body and body["webhook_url"]:
        _validate_webhook_url(body["webhook_url"])
        body["webhook_url"] = crypto.encrypt(body["webhook_url"])
    elif "webhook_url" in body and not body["webhook_url"]:
        body["webhook_url"] = None  # allow clearing

    current = json.loads(org.settings_json) if org.settings_json else {}
    for key in ALLOWED_SETTINGS_KEYS:
        if key in body:
            current[key] = body[key]

    db.execute(text("UPDATE groups SET settings_json = :s, updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
               {"s": json.dumps(current), "id": org_id})
    db.commit()

    log_audit(db, "org_settings_updated", "org", org_id, f"Settings updated for org '{org.name}'")
    return {**DEFAULT_ORG_SETTINGS, **current}
