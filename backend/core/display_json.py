"""Fail-closed JSON readers for display projections, never dispatch evidence."""
import json


def display_object(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def display_tray_state(value):
    # Preserve all decodable object fields for existing display consumers.
    # Catalogue readers validate only the fields they use.
    return display_object(value) or {}
