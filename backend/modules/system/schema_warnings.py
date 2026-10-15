"""Schema migrations deferred by existing user data (see docs/SCHEMA_MIGRATIONS.md).

Readiness reports only which migrations are deferred; the admin diagnostics bundle adds
the operator instructions and the conflicting rows.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from core.schema.bootstrap import DEFERRAL_CONFIG_KEY, DEFERRED_SUFFIX


def deferred_migrations(connection, *, detail: bool) -> list[dict]:
    markers = sorted(
        str(row[0])[: -len(DEFERRED_SUFFIX)]
        for row in connection.execute(text("SELECT migration_id FROM odin_schema_migrations WHERE migration_id LIKE :p"), {"p": "%" + DEFERRED_SUFFIX})
    )
    if not detail:
        return [{"migration": marker} for marker in markers]
    raw = connection.execute(text("SELECT value FROM system_config WHERE key=:k"), {"k": DEFERRAL_CONFIG_KEY}).scalar()
    value = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return [{"migration": marker, **(value.get(marker) or {"reason": "Deferred; reason unavailable"})} for marker in markers]
