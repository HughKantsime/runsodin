"""Pro trial ending notice (spec .claude/specs/pro-trial.md).

Delivered through the existing system-notice mechanism (GET /system/notices,
Dashboard SystemNoticesBanner), not the alerts table: it is derived from the
installed license, appears once the trial has TRIAL_NOTICE_DAYS or fewer days
left, and stays dismissed for an admin once that admin dismisses it. The
dismissal list lives in the existing system_config key-value table under a key
tied to the installed license's digest, so no schema change is needed.
"""
from typing import Optional

from core.models import SystemConfig
import license_manager

TRIAL_ENDING_KIND = "license_trial_ending"
LICENSE_LINK = "/settings?tab=license"
BUY_URL = "https://runsodin.com/pricing"


def _dismissal_key(license_sha256: str) -> str:
    return f"notice_dismissed:{TRIAL_ENDING_KIND}:{license_sha256[:32]}"


def _dismissed_user_ids(db, license_sha256: str) -> set:
    row = db.query(SystemConfig).filter(SystemConfig.key == _dismissal_key(license_sha256)).first()
    value = row.value if row is not None else None
    users = value.get("user_ids") if isinstance(value, dict) else None
    return {u for u in users if isinstance(u, int)} if isinstance(users, list) else set()


def _session():
    from core.db import SessionLocal
    return SessionLocal()


def _ending_trial() -> Optional[license_manager.LicenseInfo]:
    info = license_manager.get_license()
    if not (info.valid and info.trial and info.license_sha256):
        return None
    days = info.trial_days_left()
    if days is None or days > license_manager.TRIAL_NOTICE_DAYS:
        return None
    return info


def trial_ending_notices(user_id) -> list[dict]:
    """The trial-ending notice for this admin, unless they already dismissed it."""
    info = _ending_trial()
    if info is None:
        return []
    db = _session()
    try:
        if user_id in _dismissed_user_ids(db, info.license_sha256):
            return []
    finally:
        db.close()
    days = info.trial_days_left()
    return [{
        "kind": TRIAL_ENDING_KIND,
        "severity": "warning",
        "title": f"Pro trial ends in {days} day{'s' if days != 1 else ''}",
        "message": "When it ends, Pro features stop accepting new or changed records and your data is kept. "
                   "Activate a paid license key in Settings > License to keep Pro.",
        "link": LICENSE_LINK,
        "buy_url": BUY_URL,
        "expires_at": info.expires_at,
        "days_left": days,
        "dismissible": True,
    }]


def dismiss_trial_ending_notice(user_id) -> bool:
    """Record that this admin dismissed the current trial's notice. False when none is active."""
    info = _ending_trial()
    if info is None:
        return False
    db = _session()
    try:
        key = _dismissal_key(info.license_sha256)
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        users = sorted(_dismissed_user_ids(db, info.license_sha256) | {user_id})
        if row is None:
            db.add(SystemConfig(key=key, value={"user_ids": users}))
        else:
            row.value = {"user_ids": users}
        db.commit()
        return True
    finally:
        db.close()
