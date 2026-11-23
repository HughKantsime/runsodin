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


def is_notice(entry):
    return isinstance(entry, dict) and (entry.get('severity') == 'info' or entry.get('informational_only') is True)


def historical_hms_notice(code):
    # Older monitors recorded even described notices in last_error_code.
    return (isinstance(code, str) and re.fullmatch(r'[0-9A-Fa-f]{8}_[0-9A-Fa-f]{8}', code) is not None
            and is_notice({'severity': hms_severity(int(code[9:], 16))}))


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
