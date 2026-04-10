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


def _get_all_admin_user_ids(db: Session) -> list[int]:
    """Return user IDs with admin or operator role to receive farm-wide alerts."""
    from sqlalchemy import text
    rows = db.execute(
        text("SELECT id FROM users WHERE role IN ('admin', 'operator') AND is_active = 1")
    ).fetchall()
    return [r.id for r in rows]


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
            data={"odin_event": category, **{k: str(v) for k, v in data.items()}},
        )
        if success:
            log.debug(f"Push delivered to device {device.device_id[:8]}… [{category}]")


def _handle_event(event: Event):
    """Main event handler — routes each event type to APNs delivery."""
    mapping = _EVENT_MAP.get(event.event_type)
    if not mapping:
        return

    try:
        with SessionLocal() as db:
            user_ids = _get_all_admin_user_ids(db)
            devices = _get_devices_for_users(db, user_ids)
            if not devices:
                return
            _deliver_to_devices(devices, mapping, event.data or {})
    except Exception as e:
        log.error(f"Push fanout error for {event.event_type}: {e}", exc_info=True)


def register_subscribers(bus) -> None:
    """Register push fanout as a subscriber for all relevant event types."""
    for event_type in _EVENT_MAP:
        bus.subscribe(event_type, _handle_event)
    log.info(f"Push fanout subscribed to {len(_EVENT_MAP)} event types")
