"""Remove an erased user's identifiers from stored text and JSON (GDPR Art. 17).

Two modes, so free text is cleaned without corrupting structured values:

* Structured JSON (audit details, alert metadata): a value is replaced only
  when it exactly equals an identifier AND its key names a user
  (``IDENTIFIER_KEYS``). Values of keys like ``role`` or ``new_role`` are never
  touched even if they happen to equal an identifier. Free-text keys
  (``TEXT_KEYS`` — message, title, detail, note…) get the bounded-match
  replacement so an embedded name is scrubbed without wrecking the sentence.
* Plain text (alert title/message columns): bounded-match replacement.

Bounded match: the identifier must sit on a token boundary, so "bob" is
scrubbed in "bob logged in", "@bob", "bob." and "(bob)" but not in "bobby".
Email matching is case-insensitive (addresses are case-insensitive in
practice); other identifiers match case-sensitively.
"""
import json
import re

REPLACEMENT = "[deleted-user]"

# JSON keys whose VALUE is a user identifier (exact-match replacement).
IDENTIFIER_KEYS = frozenset({
    "username", "email", "display_name", "name", "user", "actor", "actor_username",
    "target_user", "target_username", "by", "owner", "requested_by",
})
# JSON keys whose value is free text that may embed a name (bounded replacement).
TEXT_KEYS = frozenset({"message", "title", "detail", "details", "note", "notes", "reason", "description"})
# Keys whose value must never be rewritten even if it equals an identifier.
PROTECTED_KEYS = frozenset({"role", "new_role", "old_role", "status", "action", "entity_type"})

# A token boundary is anything that is not a word char, and not the in-word
# punctuation that appears inside usernames/emails. '@' is NOT a boundary blocker
# on the left so "@bob" mentions scrub; '.' at the trailing edge is a boundary.
_WORD = r"[0-9A-Za-z_]"


def _patterns(identifiers):
    """(case-sensitive pattern, case-insensitive pattern) or (None, None)."""
    cased, lowered = [], []
    for raw in identifiers:
        if not isinstance(raw, str) or not raw:
            continue
        (lowered if "@" in raw else cased).append(raw)
    def build(items, flags):
        if not items:
            return None
        alt = "|".join(re.escape(i) for i in sorted(set(items), key=len, reverse=True))
        # Left edge: not preceded by a word char or '.' or '@' chained into a word.
        # Right edge: not followed by a word char (trailing '.' or '@' is a boundary).
        # Left edge: not inside a word or dotted token (but "@bob" mentions DO scrub).
        # Right edge: not inside a word and not an email local part ("bob@x" stays).
        return re.compile(rf"(?<![0-9A-Za-z_.])(?:{alt})(?![0-9A-Za-z_@])", flags)
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


def _exact_match(value, idents_cased, idents_lower):
    if not isinstance(value, str):
        return False
    return value in idents_cased or value.lower() in idents_lower


def _scrub_value(value, key, pats, idents_cased, idents_lower):
    if isinstance(value, dict):
        return {k: _scrub_value(v, k, pats, idents_cased, idents_lower) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v, key, pats, idents_cased, idents_lower) for v in value]
    if isinstance(value, str):
        if key in PROTECTED_KEYS:
            return value
        if key in IDENTIFIER_KEYS:
            return REPLACEMENT if _exact_match(value, idents_cased, idents_lower) else value
        if key in TEXT_KEYS or key is None:
            return _bounded_sub(value, pats)
        # Unknown key: scrub only an exact identifier value, never a partial.
        return REPLACEMENT if _exact_match(value, idents_cased, idents_lower) else value
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
