"""Portal refresh answers for "Refresh license" (spec odin-checkout-build.md B.4).

The portal answers a reactivate with one of four ``refresh_status`` values.
ODIN stores the last answer so Settings > License shows it until the next
refresh. The stored answer is bound to the digest of the installed license
file: once a different file is installed, the old answer is no longer shown.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.models import SystemConfig

log = logging.getLogger("odin.api")

REFRESH_STATUSES = ("renewed", "not_renewed_yet", "canceled", "not_entitled")
# These two answers always come with a signed file (not_entitled: one that has already ended).
FILE_REQUIRED_STATUSES = ("renewed", "not_entitled")
LAST_REFRESH_KEY = "license_last_refresh"


def refresh_status_from(data: dict) -> Optional[str]:
    """The portal's refresh_status, "renewed" for portals that predate it, None when unrecognised."""
    raw = data.get("refresh_status")
    if raw is None:
        return "renewed"
    return raw if isinstance(raw, str) and raw in REFRESH_STATUSES else None


def server_end_date(data: dict) -> Optional[str]:
    """The portal's stated end date when it sent a readable one (display only, not signed).

    The portal sends it as ``ends_at`` (canceled: when Pro stops); ``expires_at`` is read too.
    """
    from license_manager import _license_expiry_deadline
    value = data.get("ends_at") if data.get("ends_at") is not None else data.get("expires_at")
    if not isinstance(value, str):
        return None
    try:
        _license_expiry_deadline(value)
    except (ValueError, TypeError):
        return None
    return value.strip()


# The portal signs a not_entitled file to end at once (expires_at one second after
# issued_at). A server whose clock runs behind still sees it as valid; allow this
# much so the refund or chargeback still takes effect. The file then ends by itself.
NOT_ENTITLED_CLOCK_ALLOWANCE_SECONDS = 15 * 60


def ends_within_clock_allowance(candidate, now: Optional[datetime] = None) -> bool:
    """A verified, non-trial license that ends within the clock allowance from now.

    "Now" is the trusted license clock, the same one validity is judged on.
    """
    from license_manager import _license_expiry_deadline
    if not getattr(candidate, "valid", False) or getattr(candidate, "trial", False) or not candidate.expires_at:
        return False
    try:
        if now is None:
            from license_manager import license_clock
            now = license_clock().sample()[0]
        remaining = (_license_expiry_deadline(candidate.expires_at) - now).total_seconds()
    except Exception:
        # An unreadable clock or date never widens what is installed: refuse (502 upstream).
        log.warning("Clock allowance check failed; treating the file as not ended", exc_info=True)
        return False
    return remaining <= NOT_ENTITLED_CLOCK_ALLOWANCE_SECONDS


def earlier_end_date(portal: Optional[str], installed: Optional[str]) -> Optional[str]:
    """The earlier of the portal's stated end and the installed file's end (either may be missing)."""
    from license_manager import _license_expiry_deadline
    if not portal:
        return installed or None
    if not installed:
        return portal
    try:
        return portal if _license_expiry_deadline(portal) < _license_expiry_deadline(installed) else installed
    except (ValueError, TypeError):
        return installed


def _session():
    from core.db import SessionLocal
    return SessionLocal()


def _public(stored: dict) -> Dict[str, Any]:
    return {"status": stored.get("status"), "at": stored.get("at"), "expires_at": stored.get("expires_at")}


def record_last_refresh(db, status: str, expires_at: Optional[str], license_sha256: str,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    """Store the answer (replacing the previous one) and return its public form.

    A storage failure is logged, not raised: the license file may already have
    been replaced, and the admin must still see the answer in this response.
    """
    at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stored = {"status": status, "at": at, "expires_at": expires_at or None, "license_sha256": license_sha256}
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == LAST_REFRESH_KEY).first()
        if row is None:
            db.add(SystemConfig(key=LAST_REFRESH_KEY, value=stored))
        else:
            row.value = stored
        db.commit()
    except Exception:
        log.warning("Could not store the last license refresh answer", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    return _public(stored)


def read_last_refresh(license_sha256: str) -> Optional[Dict[str, Any]]:
    """The stored answer, only while it belongs to the installed license file."""
    if not license_sha256:
        return None
    try:
        db = _session()
        try:
            row = db.query(SystemConfig).filter(SystemConfig.key == LAST_REFRESH_KEY).first()
            stored = row.value if row is not None else None
        finally:
            db.close()
    except Exception:
        return None
    if not isinstance(stored, dict) or stored.get("license_sha256") != license_sha256:
        return None
    if stored.get("status") not in REFRESH_STATUSES:
        return None
    return _public(stored)
