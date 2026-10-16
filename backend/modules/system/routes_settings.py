"""System settings routes — branding, education mode, and language/i18n settings."""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel as PydanticBaseModel, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import sql
from core.dependencies import log_audit
from core.rbac import require_role, require_superadmin
from modules.organizations.branding import get_or_create_branding, branding_to_dict, effective_branding, UPDATABLE_FIELDS
from license_manager import feature_gate

log = logging.getLogger("odin.api")
router = APIRouter()

# ============== Upload Validation Helpers ==============

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

_IMAGE_TYPE_EXT = {
    "png": "png",
    "jpeg": "jpg",
    "svg": "svg",
    "webp": "webp",
}


def _detect_image_type(content: bytes) -> str | None:
    from .branding_assets import validated_image_type
    try:
        return validated_image_type(content)
    except Exception:
        return None


def _branding_directory():
    from pathlib import Path
    from core.branding_storage import branding_directory
    return branding_directory(Path(__file__).resolve().parents[2] / "static" / "branding")


# ============== Branding ==============

@router.get("/branding", tags=["Branding"])
async def get_branding(db: Session = Depends(get_db)):
    """Get the effective branding. PUBLIC - no auth required (stock branding without the Pro entitlement)."""
    return effective_branding(db)


class BrandingUpdateRequest(PydanticBaseModel):
    @model_validator(mode="before")
    @classmethod
    def validate_branding_fields(cls, data):
        import re
        from urllib.parse import urlsplit
        if not isinstance(data, dict):
            return data
        data = dict(data)
        colors = set(UPDATABLE_FIELDS) - {"app_name", "app_subtitle", "font_display", "font_body", "font_mono", "footer_text", "support_url"}
        for key, value in data.items():
            if key not in UPDATABLE_FIELDS:
                continue
            if key == "support_url":
                if value is None:
                    continue
                if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError("Invalid support URL")
                value = value.strip()
                if not value:
                    data[key] = None
                    continue
                try:
                    parsed = urlsplit(value)
                    valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname) and not any(c.isspace() for c in value)
                    parsed.port
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError("Support URL must be an HTTP or HTTPS URL with a hostname")
                data[key] = value
                continue
            if not isinstance(value, str):
                raise ValueError("Branding text and colors cannot be null")
            if key in colors:
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                    raise ValueError("Branding colors require six hexadecimal digits")
                continue
            limit = 100 if key == "app_name" else 500 if key == "footer_text" else 200
            if len(value) > limit or (key == "app_name" and not value.strip()):
                raise ValueError("Invalid branding text length")
            if key.startswith("font_") and (not value.strip() or not re.fullmatch(r"[a-zA-Z0-9 ,_'\"-]+", value)):
                raise ValueError("Invalid font family list")
        return data

    app_name: Optional[str] = None
    app_subtitle: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    sidebar_bg: Optional[str] = None
    sidebar_border: Optional[str] = None
    sidebar_text: Optional[str] = None
    sidebar_active_bg: Optional[str] = None
    sidebar_active_text: Optional[str] = None
    content_bg: Optional[str] = None
    card_bg: Optional[str] = None
    card_border: Optional[str] = None
    text_primary: Optional[str] = None
    text_secondary: Optional[str] = None
    text_muted: Optional[str] = None
    input_bg: Optional[str] = None
    input_border: Optional[str] = None
    font_display: Optional[str] = None
    font_body: Optional[str] = None
    font_mono: Optional[str] = None
    footer_text: Optional[str] = None
    support_url: Optional[str] = None


@router.put("/branding", tags=["Branding"], dependencies=[Depends(feature_gate("branding"))])
async def update_branding(data: BrandingUpdateRequest, current_user: dict = Depends(require_superadmin(scope="write")), db: Session = Depends(get_db)):
    """Update branding config. Admin only."""
    branding = get_or_create_branding(db)
    for key, value in data.model_dump(exclude_unset=True).items():
        if key in UPDATABLE_FIELDS:
            setattr(branding, key, value)
    branding.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(branding)
    return branding_to_dict(branding)


@router.post("/branding/logo", tags=["Branding"], dependencies=[Depends(feature_gate("branding"))])
async def upload_logo(file: UploadFile = File(...), current_user: dict = Depends(require_superadmin(scope="write")), db: Session = Depends(get_db)):
    """Upload brand logo. Admin only."""
    import shutil
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    detected_type = _detect_image_type(content)
    if detected_type is None:
        raise HTTPException(status_code=400, detail="File type not allowed. Upload PNG, JPEG, SVG, or WebP.")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 5 MB.")
    ext = _IMAGE_TYPE_EXT[detected_type]
    from .branding_assets import store_asset
    try:
        filename = store_asset(_branding_directory(), "logo", ext, content)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branding storage path")
    branding = get_or_create_branding(db)
    branding.logo_url = f"/static/branding/{filename}"
    db.commit()
    return {"logo_url": branding.logo_url}


@router.post("/branding/favicon", tags=["Branding"], dependencies=[Depends(feature_gate("branding"))])
async def upload_favicon(file: UploadFile = File(...), current_user: dict = Depends(require_superadmin(scope="write")), db: Session = Depends(get_db)):
    """Upload favicon. Admin only."""
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    detected_type = _detect_image_type(content)
    if detected_type is None:
        raise HTTPException(status_code=400, detail="File type not allowed. Upload PNG, JPEG, SVG, or WebP.")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 5 MB.")
    ext = _IMAGE_TYPE_EXT[detected_type]
    from .branding_assets import store_asset
    try:
        filename = store_asset(_branding_directory(), "favicon", ext, content)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branding storage path")
    branding = get_or_create_branding(db)
    branding.favicon_url = f"/static/branding/{filename}"
    db.commit()
    return {"favicon_url": branding.favicon_url}


@router.delete("/branding/logo", tags=["Branding"])
async def remove_logo(current_user: dict = Depends(require_superadmin(scope="write")), db: Session = Depends(get_db)):
    """Remove brand logo. Admin only."""
    branding = get_or_create_branding(db)
    # Logical removal retains immutable bytes for concurrent readers and failed saves.
    branding.logo_url = None
    db.commit()
    return {"logo_url": None}


@router.delete("/branding/favicon", tags=["Branding"])
async def remove_favicon(current_user: dict = Depends(require_superadmin(scope="write")), db: Session = Depends(get_db)):
    """Remove the active favicon while retaining immutable bytes for existing readers."""
    branding = get_or_create_branding(db)
    branding.favicon_url = None
    db.commit()
    return {"favicon_url": None}


# ============== Education Mode ==============

@router.get("/settings/education-mode", tags=["Settings"])
async def get_education_mode(db: Session = Depends(get_db)):
    """Get education mode status. Public (frontend needs this at load time)."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'education_mode'")).fetchone()
    return {"enabled": row[0] == "true" if row else False}


@router.put("/settings/education-mode", tags=["Settings"])
async def set_education_mode(request: Request, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Enable or disable education mode. Admin only."""
    data = await request.json()
    enabled = bool(data.get("enabled", False))
    str_val = "true" if enabled else "false"
    existing = db.execute(text("SELECT 1 FROM system_config WHERE key = 'education_mode'")).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = :v WHERE key = 'education_mode'"), {"v": str_val})
    else:
        db.execute(text("INSERT INTO system_config (key, value) VALUES ('education_mode', :v)"), {"v": str_val})
    log_audit(db, "education_mode_toggled", details=f"Education mode {'enabled' if enabled else 'disabled'}")
    db.commit()
    return {"enabled": enabled}


# ============== Language / i18n ==============

@router.get("/settings/language", tags=["Settings"])
async def get_language(db: Session = Depends(get_db)):
    """Get current interface language."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'language'")).fetchone()
    return {"language": result[0] if result else "en"}


@router.put("/settings/language", tags=["Settings"])
async def set_language(request: Request, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Set interface language."""
    data = await request.json()
    lang = data.get("language", "en")
    supported = ["en", "de", "ja", "es", "it"]
    if lang not in supported:
        raise HTTPException(400, f"Unsupported language. Choose from: {', '.join(supported)}")
    db.execute(text(f"{sql.upsert_prefix()} system_config (key, value) VALUES ('language', :lang){sql.on_conflict_suffix('key', ['value'])}"), {"lang": lang})  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    db.commit()
    return {"language": lang}
