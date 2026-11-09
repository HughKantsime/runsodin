"""Authorization caches share the existing trusted request scope lifetime."""
from weakref import WeakKeyDictionary


def request_cache(db):
    from core.audit_actor import _BINDING
    info = getattr(db, 'info', {})
    binding = info.get(_BINDING)
    if binding is not None:
        scope = binding[0]
        if not scope.live:
            return None
        caches = getattr(scope, '_controlled_caches', None)
        if caches is None:
            caches = scope._controlled_caches = WeakKeyDictionary()
        return caches.setdefault(db, {})
    # Explicitly owned short-lived sessions/connections (response projection,
    # MQTT and direct probes) can opt in without a mounted HTTP request scope.
    return info if info.get('_controlled_request') else None
