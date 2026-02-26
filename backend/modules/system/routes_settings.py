"""System settings routes — branding, education mode, and language/i18n settings."""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role
from modules.organizations.branding import get_or_create_branding, branding_to_dict, UPDATABLE_FIELDS

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Branding ==============

@router.get("/branding", tags=["Branding"])
async def get_branding(db: Session = Depends(get_db)):
    """Get branding config. PUBLIC - no auth required."""
    return branding_to_dict(get_or_create_branding(db))


@router.put("/branding", tags=["Branding"])
async def update_branding(data: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update branding config. Admin only."""
    branding = get_or_create_branding(db)
    for key, value in data.items():
        if key in UPDATABLE_FIELDS:
            setattr(branding, key, value)
    branding.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(branding)
    return branding_to_dict(branding)


@router.post("/branding/logo", tags=["Branding"])
async def upload_logo(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Upload brand logo. Admin only."""
    import shutil
    allowed = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "..", "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"logo.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.logo_url = f"/static/branding/{filename}"
    db.commit()
    return {"logo_url": branding.logo_url}


@router.post("/branding/favicon", tags=["Branding"])
async def upload_favicon(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Upload favicon. Admin only."""
    import shutil
    allowed = {"image/png", "image/x-icon", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "..", "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"favicon.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.favicon_url = f"/static/branding/{filename}"
    db.commit()
    return {"favicon_url": branding.favicon_url}


@router.delete("/branding/logo", tags=["Branding"])
async def remove_logo(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Remove brand logo. Admin only."""
    branding = get_or_create_branding(db)
    if branding.logo_url:
        filepath = os.path.join(os.path.dirname(__file__), "..", branding.logo_url.lstrip("/"))
        if os.path.exists(filepath):
            os.remove(filepath)
    branding.logo_url = None
    db.commit()
    return {"logo_url": None}


# ============== Education Mode ==============

@router.get("/settings/education-mode", tags=["Settings"])
async def get_education_mode(db: Session = Depends(get_db)):
    """Get education mode status. Public (frontend needs this at load time)."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'education_mode'")).fetchone()
    return {"enabled": row[0] == "true" if row else False}


@router.put("/settings/education-mode", tags=["Settings"])
async def set_education_mode(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Enable or disable education mode. Admin only."""
    data = await request.json()
    enabled = bool(data.get("enabled", False))
    str_val = "true" if enabled else "false"
    existing = db.execute(text("SELECT 1 FROM system_config WHERE key = 'education_mode'")).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = :v WHERE key = 'education_mode'"), {"v": str_val})
    else:
        db.execute(text("INSERT INTO system_config (key, value) VALUES ('education_mode', :v)"), {"v": str_val})
    db.commit()
    log_audit(db, "education_mode_toggled", details=f"Education mode {'enabled' if enabled else 'disabled'}")
    return {"enabled": enabled}


# ============== Language / i18n ==============

@router.get("/settings/language", tags=["Settings"])
async def get_language(db: Session = Depends(get_db)):
    """Get current interface language."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'language'")).fetchone()
    return {"language": result[0] if result else "en"}


@router.put("/settings/language", tags=["Settings"])
async def set_language(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set interface language."""
    data = await request.json()
    lang = data.get("language", "en")
    supported = ["en", "de", "ja", "es"]
    if lang not in supported:
        raise HTTPException(400, f"Unsupported language. Choose from: {', '.join(supported)}")
    db.execute(text("INSERT OR REPLACE INTO system_config (key, value) VALUES ('language', :lang)"), {"lang": lang})
    db.commit()
    return {"language": lang}
