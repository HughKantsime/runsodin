"""Shared webhook event selection contract for stored rows and API writes."""
import json


def parse_event_selection(value):
    """Return a validated selection; None preserves legacy unspecified wildcard.

    Explicit empty lists select nothing. Invalid stored configuration must never
    expand delivery, including JSON null, strings, objects, and mixed lists.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("alert_types must be an array of nonblank strings") from exc
    if not isinstance(value, list) or any(
        not isinstance(event, str) or not event.strip() for event in value
    ):
        raise ValueError("alert_types must be an array of nonblank strings")
    return value


# Event ids stored by older Settings UI builds (uppercase; HMS errors had their own id)
# mapped to the dispatched AlertType values. Read-time only: no stored data is rewritten.
LEGACY_EVENT_ALIASES = {"hms_error": "printer_error"}


def canonical_event(event):
    """Dispatched event id for a stored selection entry (case-insensitive, legacy aliases)."""
    normalized = event.strip().lower()
    return LEGACY_EVENT_ALIASES.get(normalized, normalized)


def canonical_selection(value):
    """Selection with every entry in canonical form (order kept, duplicates dropped); None stays None.

    Raises ValueError like parse_event_selection for invalid stored configuration.
    """
    selection = parse_event_selection(value)
    if selection is None:
        return None
    out = []
    for item in selection:
        canonical = canonical_event(item)
        if canonical not in out:
            out.append(canonical)
    return out


def selects_event(value, event):
    """Invalid configurations select no events; siblings continue independently."""
    try:
        selection = parse_event_selection(value)
    except ValueError:
        return False
    if selection is None:
        return True
    # Event ids are matched in canonical form: dispatched types are lowercase
    # AlertType values; older Settings UI builds stored uppercase/legacy ids.
    wanted = {canonical_event(item) for item in selection}
    return "all" in wanted or (isinstance(event, str) and canonical_event(event) in wanted)


def serialize_event_selection(value):
    selection = parse_event_selection(value)
    return None if selection is None else json.dumps(selection)
