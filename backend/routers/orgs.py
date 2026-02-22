"""O.D.I.N. — Organization Routes"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime
import json
import logging

import crypto
from deps import get_db, require_role, log_audit, _validate_webhook_url

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
            except Exception:
                pass  # already plaintext (pre-v1.3.67 rows)
        return {**DEFAULT_ORG_SETTINGS, **stored}
    return dict(DEFAULT_ORG_SETTINGS)


# =============================================================================
# Organizations CRUD
# =============================================================================

@router.get("/orgs", tags=["Organizations"])
async def list_orgs(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """List all organizations."""
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
async def create_org(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Create a new organization."""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Organization name is required")

    existing = db.execute(text("SELECT 1 FROM groups WHERE name = :name"), {"name": name}).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Organization name already exists")

    db.execute(text("""INSERT INTO groups (name, description, owner_id, is_org)
                       VALUES (:name, :desc, :owner, 1)"""),
               {"name": name, "desc": body.get("description", ""), "owner": current_user["id"]})
    db.commit()
    org_id = db.execute(text("SELECT last_insert_rowid()")).scalar()

    log_audit(db, "org_created", "org", org_id, f"Organization '{name}' created")
    return {"id": org_id, "name": name, "status": "ok"}


@router.patch("/orgs/{org_id}", tags=["Organizations"])
async def update_org(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update an organization."""
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
        db.execute(text(f"UPDATE groups SET {', '.join(sets)} WHERE id = :id"), params)
        db.commit()

    return {"status": "ok"}


@router.delete("/orgs/{org_id}", tags=["Organizations"])
async def delete_org(org_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete an organization. Unlinks members but does not delete them."""
    org = db.execute(text("SELECT * FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Unlink members
    db.execute(text("UPDATE users SET group_id = NULL WHERE group_id = :id"), {"id": org_id})
    # Unlink resources (table names from constant allowlist, not user input)
    _ORG_RESOURCE_TABLES = ("printers", "models", "spools")
    for tbl in _ORG_RESOURCE_TABLES:
        db.execute(text(f"UPDATE {tbl} SET org_id = NULL WHERE org_id = :id"), {"id": org_id})
    db.execute(text("DELETE FROM groups WHERE id = :id"), {"id": org_id})
    db.commit()

    log_audit(db, "org_deleted", "org", org_id, f"Organization '{org.name}' deleted")
    return {"status": "ok"}


@router.post("/orgs/{org_id}/members", tags=["Organizations"])
async def add_org_member(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Add a user to an organization."""
    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    org = db.execute(text("SELECT 1 FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    db.execute(text("UPDATE users SET group_id = :org_id WHERE id = :uid"), {"org_id": org_id, "uid": user_id})
    db.commit()
    return {"status": "ok"}


@router.post("/orgs/{org_id}/printers", tags=["Organizations"])
async def assign_printer_to_org(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Assign a printer to an organization."""
    printer_id = body.get("printer_id")
    db.execute(text("UPDATE printers SET org_id = :oid WHERE id = :pid"),
               {"oid": org_id, "pid": printer_id})
    db.commit()
    return {"status": "ok"}


# =============================================================================
# Organization Settings
# =============================================================================

@router.get("/orgs/{org_id}/settings", tags=["Organizations"])
async def get_org_settings(org_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get org-level settings (default filament, notifications, branding)."""
    org = db.execute(text("SELECT 1 FROM groups WHERE id = :id AND is_org = 1"), {"id": org_id}).fetchone()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _get_org_settings(db, org_id)


@router.put("/orgs/{org_id}/settings", tags=["Organizations"])
async def update_org_settings(org_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update org-level settings. Only known keys are accepted."""
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
