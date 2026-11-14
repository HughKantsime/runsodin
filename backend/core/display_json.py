"""Fail-closed JSON readers for display projections, never dispatch evidence."""
import json
import math


def display_object(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def display_tray_state(value):
    state = display_object(value)
    if not state:
        return {}
    observed = state.get('observed', {})
    if not isinstance(observed, dict):
        return {}
    if any(observed.get(k) is not None and not isinstance(observed[k], str)
           for k in ('type', 'color', 'sub_brand')):
        return {}
    if state.get('id') is not None and not isinstance(state['id'], str):
        return {}
    if 'pending' in state and type(state['pending']) is not bool:
        return {}
    remaining = state.get('remaining')
    if remaining is not None and (type(remaining) not in (int, float)
                                  or not math.isfinite(remaining) or not 0 <= remaining <= 100):
        return {}
    return state
