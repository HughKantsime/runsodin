"""
O.D.I.N. — Quota tracking helpers.

Provides period key generation and quota usage row access used by
the jobs and organizations modules for per-user quota enforcement.

Extracted from deps.py as part of the modular architecture refactor.
"""

from datetime import datetime, timezone

from sqlalchemy import text


def _get_period_key(period: str) -> str:
    """Generate a period key like '2026-02' for monthly, '2026-W07' for weekly."""
    now = datetime.now(timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    elif period == "weekly":
        return now.strftime("%Y-W%W")
    elif period == "semester":
        return f"{now.year}-S{'1' if now.month <= 6 else '2'}"
    else:  # monthly
        return now.strftime("%Y-%m")


def _get_quota_usage(db, user_id: int, period: str) -> dict:
    """Get or create quota usage row for current period."""
    key = _get_period_key(period)
    row = db.execute(
        text("SELECT * FROM quota_usage WHERE user_id = :uid AND period_key = :pk"),
        {"uid": user_id, "pk": key},
    ).fetchone()
    if row:
        return dict(row._mapping)
    db.execute(
        text("INSERT INTO quota_usage (user_id, period_key) VALUES (:uid, :pk)"),
        {"uid": user_id, "pk": key},
    )
    db.commit()
    return {"user_id": user_id, "period_key": key, "grams_used": 0, "hours_used": 0, "jobs_used": 0}
