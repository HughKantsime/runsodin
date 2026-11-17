"""Authorization caches share the existing trusted request scope lifetime."""
from weakref import WeakKeyDictionary
from contextlib import contextmanager


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


def invalidate(db):
    """Forget policy snapshots after a controlled-data write, including projections."""
    cache = request_cache(db)
    if cache is not None:
        for key in list(cache):
            name = key[0] if isinstance(key, tuple) else key
            if isinstance(name, str) and name.startswith('_controlled_') and name != '_controlled_request':
                cache.pop(key, None)


@contextmanager
def delivery_cache(connection):
    """Share reads only within one owned delivery transaction, never across commits."""
    previous = {key: value for key, value in connection.info.items()
                if isinstance(key, str) and key.startswith('_controlled_')}
    connection.info['_controlled_request'] = True
    invalidate(connection)
    try:
        yield
    finally:
        invalidate(connection)
        connection.info.pop('_controlled_request', None)
        connection.info.update(previous)
