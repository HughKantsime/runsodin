"""In-process cache of the IP allowlist policy (system_config key ``ip_allowlist``).

The allowlist check runs on every /api request. Reading the policy from the database
each time opened a connection per request; on SQLite each new connection re-parses the
whole 2.0 schema. The policy is now cached per database for a short TTL
(``ODIN_IP_ALLOWLIST_CACHE_SECONDS``, default 2 s, 0 disables the cache, at most 30 s).

Consistency: the worker that saves the allowlist invalidates its cache at once; other
API workers apply a change within the TTL. The cache fails closed: a policy that cannot
be read is never assumed, so a failed load denies the request (503) exactly as before,
and an expired entry is never served when its reload fails.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable

from sqlalchemy import text

_DEFAULT_TTL_SECONDS = 2.0
_MAX_TTL_SECONDS = 30.0


def cache_ttl_seconds() -> float:
    raw = os.environ.get("ODIN_IP_ALLOWLIST_CACHE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    value = float(raw)
    if not 0 <= value <= _MAX_TTL_SECONDS:
        raise ValueError(f"ODIN_IP_ALLOWLIST_CACHE_SECONDS must be between 0 and {_MAX_TTL_SECONDS:g}")
    return value


_lock = threading.Lock()
_entries: dict[str, tuple[float, dict | None]] = {}


def invalidate() -> None:
    """Drop every cached policy in this process (called after an allowlist write)."""
    with _lock:
        _entries.clear()


def _database_key(session) -> str:
    bind = session.get_bind()
    return str(bind.url)


def policy(session_factory: Callable, *, clock: Callable[[], float] = time.monotonic) -> dict | None:
    """The stored allowlist policy (``None`` when none is stored). Raises if it cannot be read."""
    ttl = cache_ttl_seconds()
    db = session_factory()
    try:
        key = _database_key(db)
        with _lock:
            cached = _entries.get(key)
            if ttl > 0 and cached is not None and clock() - cached[0] < ttl:
                return cached[1]
        loaded_at = clock()
        row = db.execute(text("SELECT value FROM system_config WHERE key = 'ip_allowlist'")).fetchone()
    finally:
        db.close()
    value = None
    if row:
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    with _lock:
        _entries[key] = (loaded_at, value)
    return value
