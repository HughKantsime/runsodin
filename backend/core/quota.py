"""
O.D.I.N. — Quota tracking helpers.

Provides period key generation and quota usage row access used by
the jobs and organizations modules for per-user quota enforcement.

Extracted from deps.py as part of the modular architecture refactor.
"""

import json
import re
from datetime import datetime, timezone

from sqlalchemy import text


def _get_period_key(period: str, *, now=None) -> str:
    """Generate a period key like '2026-02' for monthly, '2026-W07' for weekly.

    Weekly keys are ISO 8601 weeks (%G-W%V): Monday..Sunday always share one
    key, including the week that spans 31 December / 1 January.
    """
    now = now or datetime.now(timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    elif period == "weekly":
        return now.strftime("%G-W%V")
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
    on both supported databases. Grams/hours usage is charged when a print
    completes (record_print_consumption); a limit blocks new jobs once the
    recorded usage reaches it.
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


# ---------------------------------------------------------------------------
# Print consumption (grams / hours) charged when a job completes
# ---------------------------------------------------------------------------

def sqlalchemy_runner(db):
    """Adapt a SQLAlchemy session to the ``run(sql, params)`` shape used below."""
    return lambda statement, params: db.execute(text(statement), params)


def qmark_runner(cursor):
    """Adapt a qmark DB-API cursor (core.db_utils.get_db) to ``run(sql, params)``."""
    def run(statement, params):
        values = []

        def bind(match):
            values.append(params[match.group(1)])
            return "?"
        return cursor.execute(re.sub(r"(?<!:):([A-Za-z_]\w*)", bind, statement), tuple(values))
    return run


def _json_value(value):
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def job_material_grams(run, job_id) -> float:
    """Grams a job uses: model colour requirements, else its print file's filament list."""
    row = run("SELECT m.color_requirements FROM jobs j JOIN models m ON m.id = j.model_id "
              "WHERE j.id = :jid", {"jid": job_id}).fetchone()
    req = _json_value(row[0]) if row else None
    total = 0.0
    if isinstance(req, dict):
        for slot in req.values():
            if isinstance(slot, dict) and slot.get("grams"):
                total += float(slot["grams"])
    if total > 0:
        return total
    row = run("SELECT filaments_json FROM print_files WHERE job_id = :jid ORDER BY id LIMIT 1",
              {"jid": job_id}).fetchone()
    filaments = _json_value(row[0]) if row else None
    if isinstance(filaments, list):
        for fil in filaments:
            if isinstance(fil, dict):
                grams = fil.get("used_grams") or fil.get("weight_grams")
                if grams:
                    total += float(grams)
    return total


def _as_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


def record_print_consumption(run, job_id, *, grams=None, measured_hours=None, now=None) -> None:
    """Charge a completed job's material and print time to its owner's quota usage.

    The owner is the user the job is charged to (else its submitter). Hours are
    the measured print duration when known (monitor duration, or actual start
    to completion), otherwise the job's planned duration. Runs inside the
    caller's transaction; the caller commits.
    """
    job = run("SELECT COALESCE(charged_to_user_id, submitted_by), duration_hours, actual_start, actual_end "
              "FROM jobs WHERE id = :jid", {"jid": job_id}).fetchone()
    if not job or job[0] is None:
        return
    user_id = job[0]
    user = run("SELECT quota_period FROM users WHERE id = :uid", {"uid": user_id}).fetchone()
    if not user:
        return
    if grams is None:
        grams = job_material_grams(run, job_id)
    hours = measured_hours
    if hours is None:
        start, end = _as_datetime(job[2]), _as_datetime(job[3])
        if start and end and end > start:
            hours = (end - start).total_seconds() / 3600.0
        else:
            hours = job[1] or 0.0
    grams = max(0.0, float(grams or 0.0))
    hours = max(0.0, float(hours or 0.0))
    if grams == 0 and hours == 0:
        return
    key = _get_period_key(user[0] or "monthly", now=now)
    run("""INSERT INTO quota_usage (user_id, period_key, grams_used, hours_used, jobs_used)
        VALUES (:uid, :pk, :grams, :hours, 0)
        ON CONFLICT(user_id, period_key) DO UPDATE SET
            grams_used = COALESCE(quota_usage.grams_used, 0) + :grams,
            hours_used = COALESCE(quota_usage.hours_used, 0) + :hours,
            updated_at = CURRENT_TIMESTAMP""",
        {"uid": user_id, "pk": key, "grams": grams, "hours": hours})
