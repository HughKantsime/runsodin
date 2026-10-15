"""Remove an erased user's identifiers from stored text and JSON (GDPR Art. 17).

Matching is bounded by an identifier edge: an identifier is replaced only when
it is not flanked, on either side, by a word character or the in-identifier
punctuation ``. + - @``. So erasing ``bob`` clears "bob" in "bob logged in" and
"a@x.com, bob@x.com" but leaves "bobby", "bob-smith", "alice-bob", "bob+test"
and "bob@x.com.au" untouched. Email identifiers match case-insensitively.

In structured JSON every string value is scanned with that bounded match,
except values of enum-like keys (``PROTECTED_KEYS`` — role, new_role, status …)
which are never rewritten. A JSON object key that is itself an identifier is
renamed. Plain-text columns get the same bounded scan.
"""
import json
import re

REPLACEMENT = "[deleted-user]"

# Keys whose value must never be rewritten even if it equals an identifier.
PROTECTED_KEYS = frozenset({
    "role", "new_role", "old_role", "roles", "status", "action", "entity_type",
    "state", "auth_kind", "actor_auth_kind", "severity", "alert_type",
})
# Structural field names: these keys are never renamed even when an erased
# username happens to equal one, so a user named "username" does not corrupt
# the "username" field of every row. Their VALUES are still scrubbed.
STRUCTURAL_KEYS = frozenset({
    "username", "email", "name", "display_name", "user", "actor", "actor_username",
    "target_user", "target_username", "target_user_id", "actor_user_id",
    "anonymized_user_id", "id", "by", "owner", "requested_by", "message", "title",
    "detail", "details", "note", "notes", "reason", "description", "ip", "count",
}) | PROTECTED_KEYS

# Characters that are part of an identifier token (usernames and emails); an
# identifier only matches when not flanked by any of these.
_EDGE = r"[0-9A-Za-z_.+@-]"


def _patterns(identifiers):
    """(case-sensitive pattern, case-insensitive pattern) or (None, None)."""
    cased = [i for i in identifiers if isinstance(i, str) and i and "@" not in i]
    lowered = [i for i in identifiers if isinstance(i, str) and i and "@" in i]

    def build(items, flags):
        if not items:
            return None
        alt = "|".join(re.escape(i) for i in sorted(set(items), key=len, reverse=True))
        return re.compile(rf"(?<!{_EDGE})(?:{alt})(?!{_EDGE})", flags)

    return build(cased, 0), build(lowered, re.IGNORECASE)


def _bounded_sub(value, pats):
    cased, lowered = pats
    if cased is not None:
        value = cased.sub(REPLACEMENT, value)
    if lowered is not None:
        value = lowered.sub(REPLACEMENT, value)
    return value


def scrub_text(value, identifiers):
    if not isinstance(value, str):
        return value
    return _bounded_sub(value, _patterns(identifiers))


def _is_identifier(value, idents_cased, idents_lower):
    return isinstance(value, str) and (value in idents_cased or value.lower() in idents_lower)


def _scrub_value(value, key, pats, idents_cased, idents_lower):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            # Rename a key that is itself an erased identifier, unless it is a
            # structural field name (so an erased username equal to "username"
            # does not rename that field everywhere).
            rename = _is_identifier(k, idents_cased, idents_lower) and k not in STRUCTURAL_KEYS
            new_key = REPLACEMENT if rename else k
            out[new_key] = _scrub_value(v, k, pats, idents_cased, idents_lower)
        return out
    if isinstance(value, list):
        return [_scrub_value(v, key, pats, idents_cased, idents_lower) for v in value]
    if isinstance(value, str):
        if key in PROTECTED_KEYS:
            return value
        return _bounded_sub(value, pats)
    return value


def scrub_json_text(raw, identifiers):
    """Return scrubbed JSON text, or None when nothing changed (or not JSON)."""
    pats = _patterns(identifiers)
    if pats == (None, None) or raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    idents_cased = {i for i in identifiers if isinstance(i, str) and i and "@" not in i}
    idents_lower = {i.lower() for i in identifiers if isinstance(i, str) and i and "@" in i}
    scrubbed = _scrub_value(decoded, None, pats, idents_cased, idents_lower)
    if scrubbed == decoded:
        return None
    return json.dumps(scrubbed)


def like_patterns(identifier):
    """LIKE patterns that find an identifier in JSON text, raw or \\u-escaped."""
    escaped = json.dumps(identifier)[1:-1]
    return sorted({f"%{identifier}%", f"%{escaped}%"})
