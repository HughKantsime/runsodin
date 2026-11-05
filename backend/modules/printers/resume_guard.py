"""Human review required before unattended resume over current printer faults."""
import json
import re

from sqlalchemy import text

from core.errors import ErrorCode
from core.rbac import is_interactive_session


# Bambu HMS level is the code's high 16 bits: 1 fatal, 2 serious, 3 common, 4 info.
NOTICE_LEVELS = frozenset((3, 4))


def _code_value(item):
    """Raw MQTT entries carry an int code; parsed entries a raw_code int and an
    'AAAAAAAA_CCCCCCCC' code string. Anything else is unreadable (None)."""
    raw = item.get('raw_code', item.get('code'))
    if type(raw) is int:
        return raw if 0 <= raw <= 0xFFFFFFFF else None
    if isinstance(raw, str) and re.fullmatch(r'[0-9A-Fa-f]{8}_[0-9A-Fa-f]{8}', raw):
        return int(raw[9:], 16)
    return None


def _blocking_fault(item):
    """Fatal, serious, unknown-level or unreadable entries block; notices don't."""
    if isinstance(item, dict) and item.get('display') is False:
        item = item.get('hms_safety')
        # These are raw MQTT entries, not the parsed/string-code legacy shape.
        if not isinstance(item, dict) or type(item.get('code')) is not int:
            return True
        item = {'attr': item.get('attr'), 'code': item['code']}
    if not isinstance(item, dict):
        return True
    attr, code = item.get('attr'), _code_value(item)
    if type(attr) is not int or not 0 <= attr <= 0xFFFFFFFF or code is None:
        return True
    return (code >> 16) not in NOTICE_LEVELS


def resume_refusal(db, printer, actor):
    if is_interactive_session(actor):
        return None
    # Use current monitor state, never historical HMS events or pause text.
    try:
        active_hms = json.loads(printer.hms_errors) if printer.hms_errors else []
    except (ValueError, TypeError):
        # Unreadable fault state cannot authorize an unattended resume.
        return ErrorCode.printer_fault_active
    if active_hms is None:
        active_hms = []  # JSON null: no current faults
    if not isinstance(active_hms, list) or any(_blocking_fault(item) for item in active_hms):
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
