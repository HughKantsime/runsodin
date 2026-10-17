"""License expiry notices for superadmins (spec .claude/specs/pro-trial.md).

Delivered through the existing system-notice mechanism (GET /system/notices,
Dashboard SystemNoticesBanner), not the alerts table. Both are derived from the
installed license and dismissible per admin:

* ``license_expiring``: any valid license (paid or trial) with
  LICENSE_EXPIRY_NOTICE_DAYS (30) or fewer days left.
* ``license_trial_ending``: a valid Pro trial with TRIAL_NOTICE_DAYS (3) or
  fewer days left. In that window it replaces ``license_expiring`` for the
  trial, so the admin sees one expiry banner, not two.

A dismissal is stored in the existing system_config key-value table under a
key tied to the notice kind and the installed license's digest, so it lasts
for that license only (a renewed license warns again) and needs no schema
change.
"""
from typing import Optional

from core.models import SystemConfig
import license_manager

TRIAL_ENDING_KIND = "license_trial_ending"
EXPIRING_KIND = "license_expiring"
DISMISSIBLE_KINDS = (TRIAL_ENDING_KIND, EXPIRING_KIND)
LICENSE_LINK = "/settings?tab=license"
BUY_URL = "https://runsodin.com/pricing"
LICENSE_EXPIRY_NOTICE_DAYS = 30


def _dismissal_key(kind: str, license_sha256: str) -> str:
    return f"notice_dismissed:{kind}:{license_sha256[:32]}"


def _dismissed_user_ids(db, kind: str, license_sha256: str) -> set:
    row = db.query(SystemConfig).filter(SystemConfig.key == _dismissal_key(kind, license_sha256)).first()
    value = row.value if row is not None else None
    users = value.get("user_ids") if isinstance(value, dict) else None
    return {u for u in users if isinstance(u, int)} if isinstance(users, list) else set()


def _session():
    from core.db import SessionLocal
    return SessionLocal()


def _plural(days: int) -> str:
    return f"{days} day{'s' if days != 1 else ''}"


def _active_notice(kind: Optional[str] = None):
    """(kind, info, days) of the expiry notice the installed license calls for, or None."""
    info = license_manager.get_license()
    if not (info.valid and info.license_sha256):
        return None
    days = info.days_left()
    if days is None:
        return None
    if info.trial and days <= license_manager.TRIAL_NOTICE_DAYS:
        active = TRIAL_ENDING_KIND
    elif days <= LICENSE_EXPIRY_NOTICE_DAYS:
        active = EXPIRING_KIND
    else:
        return None
    if kind is not None and kind != active:
        return None
    return active, info, days


def _notice_body(kind: str, info, days: int) -> dict:
    if kind == TRIAL_ENDING_KIND:
        title = f"Pro trial ends in {_plural(days)}"
        message = ("When it ends, Pro features stop accepting new or changed records and your data is kept. "
                   "Activate a paid license key in Settings > License to keep Pro.")
    else:
        what = "Pro trial" if info.trial else f"{license_manager.TIERS.get(info.tier, {}).get('name', info.tier)} license"
        title = f"{what} expires in {_plural(days)}"
        message = ("When it expires, this server falls back to Community limits and your data is kept. "
                   "Install a renewed license key in Settings > License before then.")
    return {
        "kind": kind,
        "severity": "warning",
        "title": title,
        "message": message,
        "link": LICENSE_LINK,
        "buy_url": BUY_URL,
        "expires_at": info.expires_at,
        "days_left": days,
        "dismissible": True,
    }


def license_notices(user_id) -> list[dict]:
    """The license expiry notice for this admin, unless they already dismissed it."""
    active = _active_notice()
    if active is None:
        return []
    kind, info, days = active
    db = _session()
    try:
        if user_id in _dismissed_user_ids(db, kind, info.license_sha256):
            return []
    finally:
        db.close()
    return [_notice_body(kind, info, days)]


def dismiss_license_notice(kind: str, user_id) -> bool:
    """Record that this admin dismissed the current license's notice of this kind. False when none is active."""
    active = _active_notice(kind)
    if active is None:
        return False
    _, info, _ = active
    db = _session()
    try:
        key = _dismissal_key(kind, info.license_sha256)
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        users = sorted(_dismissed_user_ids(db, kind, info.license_sha256) | {user_id})
        if row is None:
            db.add(SystemConfig(key=key, value={"user_ids": users}))
        else:
            row.value = {"user_ids": users}
        db.commit()
        return True
    finally:
        db.close()
