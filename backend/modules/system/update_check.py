"""Register B88. Installation-wide opt-in and durable check scheduling."""
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import JSON, bindparam, text

from core import itar
from core.audit import log_audit
from .health_version import backend_version
from .update_releases import NOTES_URL, fetch_releases, version_number

SETTING = "update_check"
RESULT = "update_check_result"
DAY = timedelta(days=1)
ITAR_REASON = "Update checks are turned off while ITAR mode is on: this server does not contact public services."
OFF = "Off. ODIN does not check for updates."


def now():
    return datetime.now(timezone.utc)


def disabled_reason():
    if itar.is_itar_mode():
        return ITAR_REASON
    if os.getenv("ODIN_UPDATE_CHECK", "").strip().lower() == "off":
        return "Update checks are turned off by this server’s ODIN_UPDATE_CHECK setting."
    return None


def read(db, key):
    value = db.execute(text("SELECT value FROM system_config WHERE key=:key"), {"key": key}).scalar_one_or_none()
    return json.loads(value) if isinstance(value, str) else value or {}


def write(db, key, value):
    db.execute(text("INSERT INTO system_config (key, value) VALUES (:key, :value) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value").bindparams(bindparam("value", type_=JSON)),
               {"key": key, "value": value})


def lock_setting(db):
    # The upsert takes the SQLite writer lock or the PostgreSQL row lock even on first enable.
    db.execute(text("INSERT INTO system_config (key, value) VALUES (:key, '{}') "
                    "ON CONFLICT(key) DO UPDATE SET value=system_config.value"), {"key": SETTING})


def configure(db, enabled, user_id):
    reason = disabled_reason()
    if enabled and reason:
        raise HTTPException(409, reason)
    lock_setting(db)
    pref = read(db, SETTING)
    if enabled != (pref.get("enabled") is True):
        offset = pref.get("offset_minutes")
        write(db, SETTING, {
            "enabled": enabled, "enabled_by": user_id if enabled else None,
            "enabled_at": now().isoformat() if enabled else None,
            "offset_minutes": secrets.randbelow(1440) if offset is None else offset,
        })
        log_audit(db, "update_check.changed", "settings", details={"enabled": enabled})
    db.commit()
    return status(db)


def status(db):
    reason = disabled_reason()
    stored_enabled = read(db, SETTING).get("enabled") is True
    enabled = stored_enabled and reason is None
    installed = backend_version()
    result = read(db, RESULT) if enabled else {}
    newest = result.get("newest")
    installed_number = version_number(installed)
    available = bool(installed_number) and version_number(newest) > installed_number
    security = available and version_number(result.get("security_version")) > installed_number
    outcome = "unknown_installed_version" if enabled and not installed_number else result.get("outcome")
    return {"enabled": enabled, "stored_enabled": stored_enabled, "disabled_reason": reason, "installed": installed,
            "checked_at": result.get("checked_at"), "outcome": outcome,
            "newest": newest, "published_at": result.get("published_at"),
            "available": available, "security": security}


def due_slot(pref, result, at, started):
    if pref.get("enabled") is not True or at - started < timedelta(minutes=15):
        return None
    slot = at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=pref["offset_minutes"])
    if slot > at:
        slot -= DAY
    last = result.get("scheduled_at")
    attempt = result.get("attempt_at")
    last_poll = result.get("scheduled_attempt_at")
    if (slot >= datetime.fromisoformat(pref["enabled_at"])
            and (not attempt or datetime.fromisoformat(attempt) < slot)
            and (not last or datetime.fromisoformat(last) < slot)
            and (not last_poll or at - datetime.fromisoformat(last_poll) >= DAY)):
        return slot
    return None


def run_check(db, *, started=None):
    """Reserve before network I/O so competing API workers and runner ticks see the same limit."""
    reason = disabled_reason()
    manual = started is None
    if reason:
        if manual:
            raise HTTPException(409, reason)
        return
    at = now()
    if not manual:
        pref = read(db, SETTING)
        if not pref.get("enabled") or not due_slot(pref, read(db, RESULT), at, started):
            return
    lock_setting(db)
    pref = read(db, SETTING)
    previous = read(db, RESULT)
    slot = None if manual else due_slot(pref, previous, at, started)
    if not manual and not slot:
        db.rollback()
        return
    if pref.get("enabled") is not True:
        db.rollback()
        raise HTTPException(409, OFF)
    last = previous.get("attempt_at")
    if last and at - datetime.fromisoformat(last) < timedelta(minutes=1):
        db.rollback()
        if manual:
            raise HTTPException(429, "Please wait a minute before checking again.")
        return
    reserved = {"attempt_at": at.isoformat()}
    if slot:
        reserved.update(scheduled_at=slot.isoformat(), scheduled_attempt_at=at.isoformat())
    write(db, RESULT, {**previous, **reserved})
    db.commit()

    installed = backend_version()
    consent = read(db, SETTING)
    db.rollback()
    reason = disabled_reason()
    # Consent can still change between this fresh read and the send. No transaction spans I/O.
    cancelled = reason is not None or consent != pref
    answer = None if cancelled else fetch_releases(installed)
    lock_setting(db)
    current = read(db, RESULT)
    if cancelled:
        # Undo only our timestamps, retaining results or reservations from later requests.
        for key, stamp in reserved.items():
            if current.get(key) == stamp:
                if key in previous:
                    current[key] = previous[key]
                else:
                    current.pop(key, None)
        write(db, RESULT, current)
    elif current.get("attempt_at") == reserved["attempt_at"]:
        # A known, unapplied security fix can fall outside GitHub's 20-release window.
        known_fix = version_number(current.get("security_version"))
        installed_number = version_number(installed)
        if installed_number and known_fix > max(installed_number, version_number(answer.get("security_version"))):
            answer.update(security=True, security_version=current["security_version"])
            if version_number(answer.get("newest")) < known_fix:
                answer.update(newest=current["newest"], published_at=current["published_at"])
        # Failed requests contain only an outcome, preserving the last known release.
        write(db, RESULT, {**current, **answer, "checked_at": at.isoformat()})
    db.commit()
    if manual:
        if cancelled:
            raise HTTPException(409, reason or OFF)
        return status(db)


def update_notices(db, user_id):
    state = status(db)
    if not state["available"]:
        return []
    version = state["newest"]
    secure = state["security"]
    if not secure and read(db, f"update_dismissed:{user_id}").get("version") == version:
        return []
    return [{"kind": "update_available", "version": version, "severity": "critical" if secure else "warning",
             "title": f"Version {version} is available" + (" and fixes a security problem" if secure else ""),
             "message": "", "link": NOTES_URL + version, "dismissible": not secure}]


def dismiss_update(db, user_id, version):
    lock_setting(db)
    notices = update_notices(db, user_id)
    if not notices or not notices[0]["dismissible"] or notices[0]["version"] != version:
        raise HTTPException(409, "This update notice cannot be dismissed.")
    write(db, f"update_dismissed:{user_id}", {"version": version})
    db.commit()
