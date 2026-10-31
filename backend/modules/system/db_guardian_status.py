"""The guardian's status file: how /health learns the database's condition.

It sits outside the database on purpose, since the thing it reports on may be damaged. Reading
it is cheap and can't fail the health probe: anything unreadable counts as no status yet.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from modules.system.backup_service import BackupValidationError, paths_from_database_url

PENDING = "pending"
HEALTHY = "ok"
FAILED = "failed"
UNAVAILABLE = "unavailable"
STALE = "stale"
NOT_HEALTHY = frozenset({FAILED, UNAVAILABLE, STALE})
# Grace beyond two missed intervals, so a slow check or a guardian restart doesn't flap /health.
STALE_GRACE_SECONDS = 300
_MAX_STATUS_BYTES = 64 * 1024


def status_path(database: Path) -> Path:
    return database.parent / "run" / "db-guardian.json"


def offsite_path(database: Path) -> Path:
    """Written by modules.system.offsite_export when the off-server job confirms a copy."""
    return database.parent / "run" / "db-offsite.json"


def read_status(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            status = json.loads(handle.read(_MAX_STATUS_BYTES))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def integrity_view(status: dict, *, now: datetime | None = None) -> dict[str, str | None]:
    """The public summary: a state and a time, nothing about what is damaged."""
    now = now or datetime.now(timezone.utc)
    integrity = status.get("integrity") if isinstance(status.get("integrity"), dict) else {}
    checked_at = parse_time(integrity.get("checked_at"))
    interval = status.get("interval_seconds")
    limit = 2 * interval + STALE_GRACE_SECONDS if isinstance(interval, int) and interval > 0 else None
    state = integrity.get("state")
    if state == "damaged":
        # Stays failed however old: only a passing check clears it.
        return {"status": FAILED, "checked_at": integrity.get("checked_at")}
    if checked_at is None:
        started_at = parse_time(status.get("started_at"))
        overdue = started_at is not None and limit is not None and (now - started_at).total_seconds() > limit
        return {"status": STALE if overdue else PENDING, "checked_at": None}
    if limit is None or (now - checked_at).total_seconds() > limit:
        return {"status": STALE, "checked_at": integrity.get("checked_at")}
    if state == "unavailable":
        return {"status": UNAVAILABLE, "checked_at": integrity.get("checked_at")}
    return {"status": HEALTHY if state == "ok" else STALE, "checked_at": integrity.get("checked_at")}


def database_integrity(database_url: str) -> dict[str, str | None] | None:
    """The guardian's last result for /health. SQLite only; PostgreSQL has no guardian."""
    try:
        paths = paths_from_database_url(database_url)
    except BackupValidationError:
        return None
    return integrity_view(read_status(status_path(paths.database)))


def overall_status(integrity: dict[str, str | None] | None) -> str:
    return "degraded" if integrity and integrity["status"] in NOT_HEALTHY else "ok"
