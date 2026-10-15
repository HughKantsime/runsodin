"""Remove an erased user's identifiers from stored text and JSON (GDPR Art. 17).

Matching is by whole identifier, not substring: "bob" is scrubbed from
"Failed login: bob from 10.0.0.5" but not from "bobby" or "bob2", so other
users' rows stay intact. JSON is decoded and scrubbed value by value; object
keys are renamed only when a key is itself one of the identifiers and is not
a field name (so a user literally named "username" does not rename every
{"username": ...} field).
"""
import json
import re

REPLACEMENT = "[deleted-user]"
# Field names that appear as JSON keys in audit details and alerts metadata.
FIELD_NAMES = frozenset({
    "username", "email", "name", "display_name", "user", "actor", "id", "role", "details",
    "message", "title", "target_user_id", "actor_user_id", "anonymized_user_id", "ip", "status",
})
_EDGE = r"[\w.@+-]"


def _pattern(identifiers):
    ordered = sorted({i for i in identifiers if isinstance(i, str) and i}, key=len, reverse=True)
    if not ordered:
        return None
    alternatives = "|".join(re.escape(i) for i in ordered)
    return re.compile(rf"(?<!{_EDGE})(?:{alternatives})(?!{_EDGE})")


def scrub_text(value, identifiers):
    pattern = _pattern(identifiers)
    if pattern is None or not isinstance(value, str):
        return value
    return pattern.sub(REPLACEMENT, value)


def _scrub_value(value, pattern, identifiers):
    if isinstance(value, str):
        return pattern.sub(REPLACEMENT, value)
    if isinstance(value, list):
        return [_scrub_value(v, pattern, identifiers) for v in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in identifiers and key not in FIELD_NAMES:
                key = REPLACEMENT
            out[key] = _scrub_value(item, pattern, identifiers)
        return out
    return value


def scrub_json_text(raw, identifiers):
    """Return the scrubbed JSON text, or None when nothing changed (or not JSON)."""
    pattern = _pattern(identifiers)
    if pattern is None or raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    scrubbed = _scrub_value(decoded, pattern, set(identifiers))
    if scrubbed == decoded:
        return None
    return json.dumps(scrubbed)


def like_patterns(identifier):
    """LIKE patterns that find an identifier in JSON text, raw or \\u-escaped."""
    escaped = json.dumps(identifier)[1:-1]
    return sorted({f"%{identifier}%", f"%{escaped}%"})
