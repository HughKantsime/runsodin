"""Human review required before unattended resume over current printer faults."""
import json

from sqlalchemy import text

from core.errors import ErrorCode
from core.rbac import is_interactive_session


def resume_refusal(db, printer, actor):
    if is_interactive_session(actor):
        return None
    # Use current monitor state, never historical HMS events or pause text.
    try:
        active_hms = json.loads(printer.hms_errors) if printer.hms_errors else []
    except (ValueError, TypeError):
        # Unreadable fault state cannot authorize an unattended resume.
        active_hms = True
    if active_hms:
        return ErrorCode.printer_fault_active
    detection = db.execute(text(
        "SELECT 1 FROM vision_detections "
        "WHERE printer_id = :printer_id AND status = 'auto_paused' LIMIT 1"
    ), {"printer_id": printer.id}).first()
    if detection:
        return ErrorCode.detection_open
    return None


def may_suggest_pause(actor):
    """Mirror pause's scope gate; broad write does not grant agent:write."""
    scopes = actor.get("_token_scopes")
    return not scopes or bool({"admin", "agent:write"}.intersection(scopes))
