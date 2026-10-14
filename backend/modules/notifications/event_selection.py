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


def selects_event(value, event):
    """Invalid configurations select no events; siblings continue independently."""
    try:
        selection = parse_event_selection(value)
    except ValueError:
        return False
    return selection is None or event in selection or "all" in selection


def serialize_event_selection(value):
    selection = parse_event_selection(value)
    return None if selection is None else json.dumps(selection)
