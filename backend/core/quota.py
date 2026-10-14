"""
O.D.I.N. — Quota tracking helpers.

Provides period key generation and quota usage row access used by
the jobs and organizations modules for per-user quota enforcement.

Extracted from deps.py as part of the modular architecture refactor.
"""

from datetime import datetime, timezone

from sqlalchemy import text


def _get_period_key(period: str, *, now=None) -> str:
    """Generate a period key like '2026-02' for monthly, '2026-W07' for weekly."""
    now = now or datetime.now(timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    elif period == "weekly":
        return now.strftime("%Y-W%W")
    elif period == "semester":
        return f"{now.year}-S{'1' if now.month <= 6 else '2'}"
    else:  # monthly
        return now.strftime("%Y-%m")


def _get_quota_usage(db, user_id: int, period: str, *, create: bool = True) -> dict:
    """Read usage; optionally create a missing row for legacy callers."""
    key = _get_period_key(period)
    row = db.execute(
        text("SELECT * FROM quota_usage WHERE user_id = :uid AND period_key = :pk"),
        {"uid": user_id, "pk": key},
    ).fetchone()
    if row:
        return dict(row._mapping)
    if not create:
        return {"user_id": user_id, "period_key": key, "grams_used": 0, "hours_used": 0, "jobs_used": 0}
    db.execute(
        text("INSERT INTO quota_usage (user_id, period_key) VALUES (:uid, :pk)"),
        {"uid": user_id, "pk": key},
    )
    db.commit()
    return {"user_id": user_id, "period_key": key, "grams_used": 0, "hours_used": 0, "jobs_used": 0}


def charge_job_quota(db, actor, count=1):
    """Check recorded exhaustion and charge job rows in the caller transaction.

    The unique usage row and conditional upsert serialize concurrent creators
    on both supported databases. Material/time usage is observed, never estimated.
    No commit or rollback occurs here; job, audit and usage share the transaction.
    """
    if not actor or count <= 0:
        return
    limits = {name: actor.get("quota_" + name) for name in ("jobs", "grams", "hours")}
    limits = {name: value for name, value in limits.items() if value is not None and value > 0}
    if not limits:
        return
    period = actor.get("quota_period") or "monthly"
    key = _get_period_key(period)
    params = {"uid": actor["id"], "pk": key, "count": count}
    predicates = []
    for name, limit in limits.items():
        params[name] = limit
        if name == "jobs":
            predicates.append("COALESCE(quota_usage.jobs_used, 0) + :count <= :jobs")
        else:
            predicates.append(f"COALESCE(quota_usage.{name}_used, 0) < :{name}")
    # A missing row has zero recorded usage, but a batch must still fit.
    insert_guard = ":count <= :jobs" if "jobs" in limits else "1=1"
    with db.no_autoflush:
        result = db.execute(text(f"""INSERT INTO quota_usage (user_id, period_key, jobs_used)
            SELECT :uid, :pk, :count WHERE {insert_guard}
            ON CONFLICT(user_id, period_key) DO UPDATE SET
                jobs_used = COALESCE(quota_usage.jobs_used, 0) + :count,
                updated_at = CURRENT_TIMESTAMP
            WHERE {' AND '.join(predicates)}"""), params)
    if result.rowcount != 1:
        from core.errors import ErrorCode, OdinError
        raise OdinError(ErrorCode.quota_exceeded,
                        f"Print quota exceeded for this {period} period",
                        status=429, retriable=False)
