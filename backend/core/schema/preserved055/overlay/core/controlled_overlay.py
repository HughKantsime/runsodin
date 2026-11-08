"""Public overlay policy, independent of the move-only route extraction."""
from functools import wraps


def protect_overlay(router):
    from fastapi.routing import APIRoute
    for route in router.routes:
        original = route.endpoint
        @wraps(original)
        def guarded(*args, _endpoint=original, **kwargs):
            from core.controlled_redaction import printer_status
            from core.controlled_parts import allowed
            value = _endpoint(*args, **kwargs)
            db = kwargs['db']
            identifier = kwargs['printer_id']
            if not allowed(db, 'printer', identifier, None):
                value = {**value, 'camera_url': None}
            return printer_status(db, identifier, None, value)
        route.endpoint = guarded
        route.dependant.call = guarded
