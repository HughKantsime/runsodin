"""
modules/push/fanout.py — Event bus subscriber that fans push notifications out to devices.

Subscribes to all relevant ODIN events and delivers APNs pushes to registered devices.
Respects per-device quiet hours and per-category opt-out preferences.
"""

import json
import logging
from datetime import datetime, time as dtime
from typing import Optional

from sqlalchemy.orm import Session

from core.db import SessionLocal
from core.interfaces.event_bus import Event
from modules.push.apns import get_provider
from modules.push.models import PushDevice

log = logging.getLogger("push.fanout")

# Maps ODIN event types → APNs notification category + human-readable copy
_EVENT_MAP = {
    "job.completed": {
        "category": "PRINT_COMPLETE",
        "title": "Print Complete",
        "body_template": "{job_name} finished on {printer_name}",
    },
    "job.failed": {
        "category": "PRINT_FAILED",
        "title": "Print Failed",
        "body_template": "{job_name} failed on {printer_name}",
    },
    "vision.detection": {
        "category": "SPAGHETTI_DETECTED",
        "title": "Failure Detected",
        "body_template": "{detection_type} detected on {printer_name} ({confidence}% confidence)",
    },
    "vision.first_layer": {
        "category": "FIRST_LAYER_ISSUE",
        "title": "First Layer Issue",
        "body_template": "First layer problem detected on {printer_name}",
    },
    "inventory.spool_low": {
        "category": "SPOOL_LOW",
        "title": "Spool Running Low",
        "body_template": "{spool_name} has {remaining_grams}g remaining",
    },
    "job.approval_required": {
        "category": "JOB_APPROVAL_REQUIRED",
        "title": "Job Awaiting Approval",
        "body_template": '{username} submitted "{job_name}"',
    },
    "printer.hms_code": {
        "category": "HMS_ERROR",
        "title": "Printer Error",
        "body_template": "{printer_name}: {message}",
    },
    "printer.disconnected": {
        "category": "PRINTER_OFFLINE",
        "title": "Printer Offline",
        "body_template": "{printer_name} is no longer reachable",
    },
}


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

    # Build body from template + event data (best-effort substitution)
    try:
        body = mapping["body_template"].format(**{k: str(v) for k, v in data.items()})
    except KeyError:
        body = mapping["body_template"]

    for device in devices:
        prefs = None
        if device.preferences_json:
            try:
                prefs = json.loads(device.preferences_json)
            except Exception:
                pass

        if _is_in_quiet_hours(prefs):
            log.debug(f"Skipping push to device {device.device_id} — quiet hours")
            continue

        if not _category_enabled(prefs, category):
            log.debug(f"Skipping push to device {device.device_id} — category {category} disabled")
            continue

        sandbox = device.platform == "apns-sandbox"
        provider = get_provider(sandbox=sandbox)
        success = provider.send_push(
            device_token=device.token,
            title=title,
            body=body,
            category=category,
            data={"odin_event": category, **{k: str(v) for k, v in data.items()
                  if k in {"printer_id", "job_id", "spool_id", "alert_type", "severity"} and v is not None}},
        )
        if success:
            log.debug(f"Push delivered to device {device.device_id[:8]}… [{category}]")


def deliver_alert(db, plan):
    """Called after the alert outbox commit; scope is rechecked at delivery time."""
    from sqlalchemy import text
    from core.recipient_policy import resolve_alert_scope
    if plan.suppress_external:
        return
    eligible, org_id = resolve_alert_scope(db, printer_id=plan.printer_id,
        job_id=plan.job_id, spool_id=plan.spool_id)
    if org_id != plan.org_id:
        return
    atype = plan.alert_type.value
    # Use the in-app recipients selected by stage_alert, then recheck preferences.
    # This also preserves Education suppression, which produces no such plan.
    users = {row[0] for row in db.execute(text(
        "SELECT user_id FROM alert_preferences WHERE UPPER(alert_type)=:atype AND in_app IS TRUE"),
        {"atype": atype.upper()})}
    recipients = eligible & users & set(plan.in_app_users)
    devices = _get_devices_for_users(db, sorted(recipients))
    mapping = {"title": "ODIN notification", "category": atype.upper(),
               "body_template": "Open ODIN to view this alert"}
    _deliver_to_devices(devices, mapping, {
        "alert_type": atype, "severity": plan.severity.value,
        "job_id": plan.job_id, "printer_id": plan.printer_id, "spool_id": plan.spool_id})


def register_subscribers(drain):
    """Register on the durable drain, never the process-local event bus."""
    drain.register_delivery(deliver_alert)
