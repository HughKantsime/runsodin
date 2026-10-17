"""O.D.I.N. — Spoolman URL (saved in Settings > General).

Stored in system_config (key ``spoolman_url``) so a save applies at once in
the API and in every daemon, like the scheduler blackout hours; a saved empty
value disables the integration. A non-empty SPOOLMAN_URL environment variable
is a deployment override: it wins, and the API refuses to change the saved
value while it is set (the same precedence as VAPID_PRIVATE_KEY).
"""

import json
from typing import Optional

from core.config import settings

SPOOLMAN_KEY = "spoolman_url"  # also spelled out in the raw query below


def _decode(value) -> Optional[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return value if isinstance(value, str) else None


def managed_by_environment() -> bool:
    """True when SPOOLMAN_URL is set in the environment (it overrides the saved value)."""
    return bool((settings.spoolman_url or "").strip())


def get_spoolman_url(db=None, conn=None) -> str:
    """Effective Spoolman base URL ('' when the integration is disabled).

    ``db`` is an ORM session and ``conn`` a raw cursor-compatible connection;
    with neither, the value is read through core.db_utils.get_db.
    """
    if managed_by_environment():
        return settings.spoolman_url.strip()
    if db is not None:
        from core.models import SystemConfig
        row = db.query(SystemConfig).filter(SystemConfig.key == SPOOLMAN_KEY).first()
        stored = _decode(row.value) if row else None
    elif conn is not None:
        cur = conn.cursor()
        # Constant key literal (no driver-specific placeholder needed on SQLite or PostgreSQL).
        cur.execute("SELECT value FROM system_config WHERE key = 'spoolman_url'")
        row = cur.fetchone()
        stored = _decode(row[0]) if row else None
    else:
        from core.db_live import live_get_db
        with live_get_db() as raw:
            return get_spoolman_url(conn=raw)
    return (stored or "").strip()


def set_spoolman_url(db, url: str) -> None:
    """Save the URL ('' disables the integration); the caller commits."""
    from core.models import SystemConfig
    value = (url or "").strip()
    row = db.query(SystemConfig).filter(SystemConfig.key == SPOOLMAN_KEY).first()
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=SPOOLMAN_KEY, value=value))
