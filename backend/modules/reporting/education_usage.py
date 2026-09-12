"""Education usage-report query service kept outside the route module."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.base import JobStatus


def build_education_usage_report(days: int, db: Session, current_user: dict) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    params = {
        "cutoff": cutoff,
        "completed": JobStatus.COMPLETED.value,
        "failed": JobStatus.FAILED.value,
    }
    user_scope = ""
    if current_user.get("role") != "admin":
        user_group_id = current_user.get("group_id")
        if user_group_id is None:
            return {
                "summary": {
                    "total_users_active": 0,
                    "total_print_hours": 0,
                    "total_jobs": 0,
                    "approval_rate": 0,
                    "rejection_rate": 0,
                },
                "users": [],
                "daily_submissions": {},
                "days": days,
            }
        user_scope = " AND u.group_id = :group_id"
        params["group_id"] = user_group_id

    # The only SQL fragment is selected from this closed role branch; every
    # runtime value remains a bound parameter.
    aggregate_rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            """
            SELECT u.id AS user_id, u.username, u.email, u.role,
                   COUNT(j.id) AS submitted,
                   SUM(CASE WHEN j.approved_by IS NOT NULL AND j.rejected_reason IS NULL THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN j.rejected_reason IS NOT NULL THEN 1 ELSE 0 END) AS rejected,
                   SUM(CASE WHEN j.status = :completed THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN j.status = :failed THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN j.status = :completed
                       THEN COALESCE(j.duration_hours, m.build_time_hours, 0) * COALESCE(j.quantity, 1)
                       ELSE 0 END) AS hours,
                   MAX(j.created_at) AS last_activity
            FROM users u
            JOIN jobs j ON j.submitted_by = u.id AND j.created_at >= :cutoff
            LEFT JOIN models m ON m.id = j.model_id
            WHERE 1 = 1
            """
            + user_scope
            + """
            GROUP BY u.id, u.username, u.email, u.role
            ORDER BY submitted DESC, u.id ASC
            """
        ),
        params,
    ).fetchall()

    # Parse only completed model JSON rows for SQLite/PostgreSQL portability.
    filament_rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            """
            SELECT j.submitted_by, j.quantity, m.color_requirements
            FROM jobs j
            JOIN users u ON u.id = j.submitted_by
            JOIN models m ON m.id = j.model_id
            WHERE j.created_at >= :cutoff AND j.status = :completed
            """
            + user_scope
        ),
        params,
    ).fetchall()
    grams_by_user: dict[int, float] = {}
    for row in filament_rows:
        requirements = row.color_requirements
        if isinstance(requirements, str):
            try:
                requirements = json.loads(requirements)
            except (TypeError, ValueError):
                requirements = {}
        grams = sum(
            float(item.get("grams", 0) or 0)
            for item in (requirements or {}).values()
            if isinstance(item, dict)
        )
        grams_by_user[row.submitted_by] = (
            grams_by_user.get(row.submitted_by, 0) + grams * (row.quantity or 1)
        )

    user_stats = []
    fleet_hours = fleet_jobs = fleet_approved = fleet_rejected = 0
    for row in aggregate_rows:
        submitted = int(row.submitted or 0)
        approved = int(row.approved or 0)
        rejected = int(row.rejected or 0)
        completed = int(row.completed or 0)
        failed = int(row.failed or 0)
        hours = float(row.hours or 0)
        last_activity = row.last_activity
        if last_activity and hasattr(last_activity, "isoformat"):
            last_activity = last_activity.isoformat()
        user_stats.append({
            "user_id": row.user_id,
            "username": row.username,
            "email": row.email,
            "role": row.role,
            "total_jobs_submitted": submitted,
            "total_jobs_approved": approved,
            "total_jobs_rejected": rejected,
            "total_jobs_completed": completed,
            "total_jobs_failed": failed,
            "total_print_hours": round(hours, 1),
            "total_filament_grams": round(grams_by_user.get(row.user_id, 0), 1),
            "approval_rate": round(approved / submitted * 100, 1) if submitted else 0,
            "success_rate": round(completed / (completed + failed) * 100, 1) if (completed + failed) else 0,
            "last_activity": last_activity,
        })
        fleet_hours += hours
        fleet_jobs += submitted
        fleet_approved += approved
        fleet_rejected += rejected

    daily_rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            """
            SELECT DATE(j.created_at) AS day, COUNT(*) AS submissions
            FROM jobs j
            JOIN users u ON u.id = j.submitted_by
            WHERE j.created_at >= :cutoff
            """
            + user_scope
            + " GROUP BY DATE(j.created_at) ORDER BY day"
        ),
        params,
    ).fetchall()
    daily = {str(row.day): int(row.submissions) for row in daily_rows if row.day}
    return {
        "summary": {
            "total_users_active": len(user_stats),
            "total_print_hours": round(fleet_hours, 1),
            "total_jobs": fleet_jobs,
            "approval_rate": round(fleet_approved / fleet_jobs * 100, 1) if fleet_jobs else 0,
            "rejection_rate": round(fleet_rejected / fleet_jobs * 100, 1) if fleet_jobs else 0,
        },
        "users": user_stats,
        "daily_submissions": daily,
        "days": days,
    }
