"""Display and safety records share the existing printer-owned JSON column.

Safety envelopes retain packets verbatim, including non-object/malformed entries.
They never enter display, history, alert deduplication or terminal handling.
"""
import json


def display_entries(entries):
    return [e for e in entries if not isinstance(e, dict) or e.get('display') is not False]


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
        return bool(display_entries(entries)) if isinstance(entries, list) else True
    except (TypeError, ValueError):
        return True
