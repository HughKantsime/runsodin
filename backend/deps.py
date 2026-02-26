"""
O.D.I.N. — Shared dependencies for API routers.

Extracted from main.py to enable clean router module imports
without circular dependencies.

Re-export facade: core modules now live under backend/core/.
All existing `from deps import X` paths continue to work.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import hmac
import time as _time
import os
import json
import logging

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

import auth as auth_module
from auth import verify_password, decode_token, has_permission, hash_password, create_access_token
from auth import Token, UserCreate, UserResponse
from models import (
    AuditLog, Base, Printer, Spool, SpoolUsage, SpoolStatus,
    FilamentSlot, Model, Job, JobStatus, FilamentType, FilamentLibrary,
    SchedulerRun, Alert, AlertPreference, AlertType, AlertSeverity,
    PushSubscription, MaintenanceTask, MaintenanceLog, SystemConfig,
    NozzleLifecycle, DryingLog, HYGROSCOPIC_TYPES, PrintPreset, Timelapse,
    VisionDetection, VisionSettings, VisionModel,
)
from config import settings
import crypto

# ── Core re-exports (database layer) ──────────────────────────────────────────
from core.db import engine, SessionLocal, Base, get_db  # noqa: F401,F811

log = logging.getLogger("odin.api")
logger = log  # alias used in some places


# ── Core re-exports (auth dependencies) ───────────────────────────────────────
from core.dependencies import get_current_user, log_audit, oauth2_scheme  # noqa: F401

# ── Core re-exports (RBAC and org scoping) ────────────────────────────────────
from core.rbac import (  # noqa: F401
    require_role,
    require_scope,
    _get_org_filter,
    get_org_scope,
    check_org_access,
)


def _validate_webhook_url(url: str) -> None:
    """Validate a webhook URL is not targeting internal infrastructure (SSRF prevention).

    Allows http:// and https:// schemes only.
    Rejects loopback, link-local, and RFC-1918 private addresses.
    Raises HTTPException 400 if the URL is invalid or targets a blocked host.
    """
    import ipaddress as _ipaddress
    import urllib.parse as _urllib_parse

    if not url:
        return

    try:
        parsed = _urllib_parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Webhook URL must use http:// or https:// scheme")

    host = parsed.hostname or ""
    blocked_prefixes = ("localhost", "127.", "169.254.", "0.", "::1")
    if any(host.startswith(p) for p in blocked_prefixes):
        raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")

    try:
        addr = _ipaddress.ip_address(host)
        if addr.is_loopback or addr.is_link_local or addr.is_private:
            raise HTTPException(status_code=400, detail="Webhook URL targets a blocked host")
    except ValueError:
        pass  # hostname — allow (DNS resolution happens at dispatch time)


def compute_printer_online(printer_dict):
    """Add is_online field based on last_seen within 90 seconds."""
    if printer_dict.get("last_seen"):
        try:
            last = datetime.fromisoformat(str(printer_dict["last_seen"]))
            printer_dict["is_online"] = (datetime.now(timezone.utc) - last).total_seconds() < 90
        except Exception:
            printer_dict["is_online"] = False
    else:
        printer_dict["is_online"] = False
    return printer_dict


def get_printer_api_key(printer: Printer) -> Optional[str]:
    """Get decrypted API key for a printer."""
    if not printer.api_key:
        return None
    return crypto.decrypt(printer.api_key)



# ── Core re-exports (auth helpers) ────────────────────────────────────────────
from core.auth_helpers import (  # noqa: F401
    _validate_password,
    _check_rate_limit,
    _record_login_attempt,
    _is_locked_out,
    _login_db_path,
    _LOGIN_RATE_LIMIT,
    _LOGIN_RATE_WINDOW,
    _LOCKOUT_THRESHOLD,
    _LOCKOUT_DURATION,
)


# ──────────────────────────────────────────────
# Quota Helpers
# ──────────────────────────────────────────────

def _get_period_key(period: str) -> str:
    """Generate a period key like '2026-02' for monthly, '2026-W07' for weekly."""
    now = datetime.now(timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    elif period == "weekly":
        return now.strftime("%Y-W%W")
    elif period == "semester":
        return f"{now.year}-S{'1' if now.month <= 6 else '2'}"
    else:  # monthly
        return now.strftime("%Y-%m")


def _get_quota_usage(db, user_id, period):
    """Get or create quota usage row for current period."""
    key = _get_period_key(period)
    row = db.execute(text("SELECT * FROM quota_usage WHERE user_id = :uid AND period_key = :pk"),
                     {"uid": user_id, "pk": key}).fetchone()
    if row:
        return dict(row._mapping)
    db.execute(text("INSERT INTO quota_usage (user_id, period_key) VALUES (:uid, :pk)"),
               {"uid": user_id, "pk": key})
    db.commit()
    return {"user_id": user_id, "period_key": key, "grams_used": 0, "hours_used": 0, "jobs_used": 0}
