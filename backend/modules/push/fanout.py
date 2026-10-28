"""
modules/push/fanout.py — After-commit native push delivery to registered devices.
Respects per-device quiet hours and per-category opt-out preferences.
"""

import json
import logging
from datetime import datetime, time as dtime
from typing import Optional

from sqlalchemy.orm import Session

from modules.push.apns import get_provider, token_fingerprint
from modules.push.models import PushDevice

from core.alert_preferences import preferences_for
from modules.push.policy import CATEGORIES, education_scope, safe_copy

log = logging.getLogger("push.fanout")


def _get_devices_for_users(db: Session, user_ids: list[int]) -> list[PushDevice]:
    """Return all registered push devices for the given user IDs."""
    if not user_ids:
        return []
    return db.query(PushDevice).filter(PushDevice.user_id.in_(user_ids)).all()


def _is_in_quiet_hours(prefs: Optional[dict]) -> bool:
    """Return True if current local time falls within the device's quiet hours window."""
    if not prefs:
        return False
    if not prefs.get("quiet_hours_enabled"):
        return False
    try:
        now = datetime.now().time()
        start = dtime(*map(int, prefs["quiet_hours_start"].split(":")))
        end = dtime(*map(int, prefs["quiet_hours_end"].split(":")))
        if start <= end:
            return start <= now <= end
        # Overnight window (e.g., 22:00 – 08:00)
        return now >= start or now <= end
    except Exception:
        return False


def _category_enabled(prefs: Optional[dict], category: str) -> bool:
    if not prefs:
        return True
    cats = prefs.get("categories", {})
    return cats.get(category, True)


def _deliver_to_devices(devices: list[PushDevice], mapping: dict, data: dict):
    """Fan out a notification to all eligible devices."""
    title = mapping["title"]
    category = mapping["category"]

    body = mapping["body"]

    for device in devices:
        prefs = None
        if device.preferences_json:
            try:
                prefs = json.loads(device.preferences_json)
            except Exception:
                pass

        if _is_in_quiet_hours(prefs):
            log.info("Push skip reason=quiet_hours category=%s alert_type=%s token_fingerprint=%s", category, data.get("alert_type"), token_fingerprint(device.token))
            continue

        if not _category_enabled(prefs, category):
            log.info("Push skip reason=category_off category=%s alert_type=%s token_fingerprint=%s", category, data.get("alert_type"), token_fingerprint(device.token))
            continue

        sandbox = device.platform == "apns-sandbox"
        provider = get_provider(sandbox=sandbox)
        result = provider.send_push_result(
            device_token=device.token,
            title=title,
            body=body,
            category=category,
            data={"odin_event": category, **{k: str(v) for k, v in data.items()
                  if k in {"printer_id", "job_id", "spool_id", "alert_type", "severity"} and v is not None}},
        )
        if result.status == "not_configured":
            log.info("Push skip reason=not_configured category=%s alert_type=%s token_fingerprint=%s", category, data.get("alert_type"), token_fingerprint(device.token))


def deliver_alert(db, plan):
    """Called for committed alerts; scope and preferences are rechecked at delivery."""
    from core.recipient_policy import resolve_alert_scope
    atype = plan.alert_type.value
    category = CATEGORIES.get(atype, "")
    if plan.suppress_external:
        log.info("Push skip reason=quiet_hours_or_deduplicated category=%s alert_type=%s", category, atype)
        return
    eligible, org_id = resolve_alert_scope(db, printer_id=plan.printer_id,
        job_id=plan.job_id, spool_id=plan.spool_id)
    if org_id != plan.org_id:
        log.info("Push skip reason=org_mismatch category=%s alert_type=%s", category, atype)
        return
    try:
        suppressed = education_scope(db, org_id=org_id, job_id=plan.job_id, printer_id=plan.printer_id)
    except Exception:
        suppressed = True
    if suppressed:
        log.info("Push skip reason=education_scope category=%s alert_type=%s", category, atype)
        return
    # Use the in-app recipients selected by stage_alert, then recheck preferences.
    # Education is checked explicitly because operational alerts can have no job.
    users = {p.user_id for p in preferences_for(db, atype) if p.in_app}
    recipients = eligible & users & set(plan.in_app_users)
    if (set(plan.in_app_users) & eligible) - users or not plan.in_app_users:
        log.info("Push skip reason=preference_off category=%s alert_type=%s", category, atype)
    if set(plan.in_app_users) - eligible:
        log.info("Push skip reason=org_mismatch_or_inactive category=%s alert_type=%s", category, atype)
    devices = _get_devices_for_users(db, sorted(recipients))
    if not devices:
        log.info("Push skip reason=no_devices category=%s alert_type=%s", category, atype)
    mapping = {"title": safe_copy(getattr(plan, "title", "ODIN notification")),
               "category": CATEGORIES.get(atype, ""),
               "body": safe_copy(getattr(plan, "message", "Open ODIN to view this alert"))[:180]}
    _deliver_to_devices(devices, mapping, {
        "alert_type": atype, "severity": plan.severity.value,
        "job_id": plan.job_id, "printer_id": plan.printer_id, "spool_id": plan.spool_id})


def register_subscribers(drain):
    """Register on the shared after-commit delivery registry, never the event bus."""
    drain.register_delivery(deliver_alert)
