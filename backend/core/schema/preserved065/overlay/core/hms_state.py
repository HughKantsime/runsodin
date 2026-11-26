"""Display and safety records share the existing printer-owned JSON column.

Safety envelopes retain packets verbatim, including non-object/malformed entries.
They never enter display, history, alert deduplication or terminal handling.
"""
import json
import re


def display_entries(entries):
    return [e for e in entries if not isinstance(e, dict) or e.get('display') is not False]


def hms_severity(raw_code):
    return {1: 'critical', 2: 'error', 3: 'info', 4: 'info'}.get(raw_code >> 16, 'unknown')


def raw_word(entry):
    for key in ('raw_code', 'code'):
        value = entry.get(key)
        if type(value) is int and 0 <= value <= 0xFFFFFFFF:
            return value
    return None


def hms_code_parts(code):
    if isinstance(code, str) and re.fullmatch(r'[0-9A-Fa-f]{8}_[0-9A-Fa-f]{8}', code):
        return int(code[:8], 16), int(code[9:], 16)
    return None, None


def stored_severity(entry):
    if not isinstance(entry, dict):
        return 'unknown'
    severity = entry.get('severity')
    if entry.get('informational_only') is True:
        return 'info'
    if severity in ('critical', 'error', 'warning', 'info'):
        return severity
    raw = raw_word(entry)
    if raw is None:
        _, raw = hms_code_parts(entry.get('code'))
    return hms_severity(raw) if raw is not None else 'unknown'


def is_notice(entry):
    return stored_severity(entry) == 'info'


def historical_hms_notice(code):
    # Older monitors recorded even described notices in last_error_code.
    return isinstance(code, str) and is_notice({'code': code})


def distinct_entries(entries):
    """Prefer a fault when old and new representations of one code coexist."""
    shown = {}
    for index, entry in enumerate(display_entries(entries)):
        key = index  # Entries without an identity stay separate.
        if isinstance(entry, dict):
            code, attr, raw = entry.get('code'), entry.get('attr'), raw_word(entry)
            if raw is not None and type(attr) is int and 0 <= attr <= 0xFFFFFFFF:
                key = f"{attr:08X}_{raw:08X}"
            elif isinstance(code, str):
                key = code.upper()
            elif raw is not None:
                key = (None, raw)
        if key not in shown or not is_notice(entry) or is_notice(shown[key]):
            shown[key] = entry
    return list(shown.values())


def attention_entries(entries):
    return [e for e in display_entries(entries) if not is_notice(e)]


def safety_entries(raw, displayed=()):
    """Retain anything the display parser did not represent, without guessing."""
    represented = {(e['attr'], e['raw_code']) for e in displayed}
    return [dict(display=False, hms_safety=e) for e in raw
            if not (isinstance(e, dict)
                    and type(e.get('attr')) is int and type(e.get('code')) is int
                    and (e['attr'], e['code']) in represented)]


def has_display_entries(value):
    """Education's existing diagnostic indication excludes safety-only data."""
    if not value:
        return False
    try:
        entries = json.loads(value)
        return bool(attention_entries(entries)) if isinstance(entries, list) else True
    except (TypeError, ValueError):
        return True
