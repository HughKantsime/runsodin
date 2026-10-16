"""O.D.I.N. — Chargeback Reports and Report Schedules."""

from core.rbac import should_filter_org

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import date, datetime, time, timedelta, timezone
import re
import json
import logging

from core.db import get_db
from modules.reporting.recipient_policy import normalize_recipients
from core.db_compat import execute_insert_returning_id, sql
from core.rbac import require_role, require_superadmin, get_org_scope

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Reports"])

REPORT_TYPES = ["fleet_utilization", "job_summary", "filament_consumption", "failure_analysis", "chargeback_summary"]


def _validated_schedule_fields(body, *, partial=False):
    """Validate before any write; apply the same rules to create and edit."""
    normalized=dict(body)
    if not partial or 'name' in body:
        name=body.get('name')
        if not isinstance(name,str) or not name.strip() or len(name)>200 or any(c in name for c in ('\r','\n')):
            raise HTTPException(400,'Report name must be a nonempty single line')
        normalized['name']=name.strip()
    if not partial or 'frequency' in body:
        frequency=body.get('frequency','weekly')
        if frequency not in ('daily','weekly','monthly'):
            raise HTTPException(400,'Frequency must be daily, weekly, or monthly')
        normalized['frequency']=frequency
    if not partial or 'recipients' in body:
        try:
            normalized['recipients']=normalize_recipients(body.get('recipients'))
        except ValueError:
            raise HTTPException(400,'Each report recipient must contain one mailbox') from None
    if not partial or 'report_type' in body:
        if body.get('report_type') not in REPORT_TYPES:
            raise HTTPException(400,f"Invalid report type. Valid: {', '.join(REPORT_TYPES)}")
    if 'is_active' in body and not isinstance(body['is_active'],bool):
        raise HTTPException(400,'Active must be a boolean')
    if 'filters' in body and not isinstance(body['filters'],dict):
        raise HTTPException(400,'Filters must be an object')
    return normalized


# ============== Chargeback Report ==============

from modules.reporting.chargeback_sql import ARCHIVE_GRAMS_BY_JOB_SQL

def _chargeback_date(value):
    if value is None:
        return None
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise HTTPException(422, "Dates must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "Invalid calendar date") from None


@router.get("/reports/chargebacks")
async def chargeback_report(
    start_date: str = None, end_date: str = None,
    current_user: dict = Depends(require_role("admin", scope="read:analytics")),
    db: Session = Depends(get_db)
):
    """Generate chargeback report — cost summary by user (org-scoped)."""
    start_day, end_day = _chargeback_date(start_date), _chargeback_date(end_date)
    if start_day is not None and end_day is not None and start_day > end_day:
        raise HTTPException(422, "Start date must not follow end date")
    org = current_user.get("group_id")
    platform = current_user.get("role") == "admin" and org is None
    if not platform and (type(org) is not int or org <= 0):
        return []
    query = """
        SELECT j.charged_to_user_id as user_id, u.username,
               COUNT(*) as job_count,
               SUM(j.estimated_cost) as total_cost,
               SUM(CASE WHEN j.estimated_cost IS NULL THEN 1 ELSE 0 END) as unknown_cost_jobs,
               SUM(j.duration_hours) as total_hours,
               SUM(pa.grams) as total_grams,
               SUM(CASE WHEN pa.job_id IS NULL THEN 1 ELSE pa.unmeasured END) as grams_unknown
        FROM jobs j
        LEFT JOIN users u ON j.charged_to_user_id = u.id
        LEFT JOIN ({_ARCHIVE_GRAMS_SQL}) pa ON pa.job_id = j.id
        WHERE j.charged_to_user_id IS NOT NULL
          AND j.status IN ('completed', 'failed')
    """.format(_ARCHIVE_GRAMS_SQL=ARCHIVE_GRAMS_BY_JOB_SQL)
    params = {}
    if not platform:
        query += " AND j.charged_to_org_id = :org"
        params["org"] = org
    # Canonical job timestamps are naive UTC. Calendar end dates include
    # the complete selected UTC day without session-timezone conversion.
    if start_day is not None:
        query += " AND j.created_at >= :start"
        params["start"] = datetime.combine(start_day, time.min)
    if end_day is not None:
        if end_day == date.max:
            query += " AND j.created_at <= :end"
            params["end"] = datetime.max
        else:
            query += " AND j.created_at < :end"
            params["end"] = datetime.combine(end_day + timedelta(days=1), time.min)
    query += " GROUP BY j.charged_to_user_id, u.username ORDER BY total_cost DESC"

    rows = db.execute(text(query), params).fetchall()
    from modules.reporting.education_usage import report_currency
    currency = None if platform else report_currency(db, current_user)
    return [{
        "user_id": r.user_id, "username": r.username or f"[user-{r.user_id}]",
        "job_count": r.job_count,
        # Only completed and failed jobs consumed resources; unpriced jobs are counted.
        "total_cost": round(r.total_cost, 2) if r.total_cost is not None else None,
        "unknown_cost_jobs": int(r.unknown_cost_jobs or 0),
        "currency": currency,
        "cost_basis": "job_estimate_current_currency",
        "total_hours": round(r.total_hours or 0, 1),
        # Archive-recorded filament: measured by the printer when reported, else the
        # sliced file's estimate. Jobs or attempts with neither are counted.
        "total_grams": round(r.total_grams, 1) if r.total_grams is not None else None,
        "grams_unknown": int(r.grams_unknown or 0),
        "grams_basis": "recorded_measured_or_file_estimate",
    } for r in rows]


# ============== Report Schedules ==============

@router.get("/report-schedules")
async def list_report_schedules(current_user: dict = Depends(require_superadmin(scope="admin")), db: Session = Depends(get_db)):
    """List all scheduled reports. Superadmin only — system-wide report config."""
    rows = db.execute(text("SELECT * FROM report_schedules ORDER BY created_at DESC")).fetchall()
    return [{
        "id": r.id, "name": r.name, "report_type": r.report_type,
        "frequency": r.frequency, "recipients": json.loads(r.recipients) if r.recipients else [],
        "filters": json.loads(r.filters) if r.filters else {},
        "is_active": bool(r.is_active), "next_run_at": r.next_run_at,
        "last_run_at": r.last_run_at, "created_at": r.created_at,
    } for r in rows]


@router.post("/report-schedules")
async def create_report_schedule(body: dict, current_user: dict = Depends(require_superadmin(scope="admin")), db: Session = Depends(get_db)):
    """Create a new scheduled report. Superadmin only."""
    body = _validated_schedule_fields(body)
    name = body["name"]
    report_type = body.get("report_type", "")
    frequency = body.get("frequency", "weekly")
    recipients = body.get("recipients", [])

    if not name:
        raise HTTPException(status_code=400, detail="Report name is required")
    if report_type not in REPORT_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid report type. Valid: {', '.join(REPORT_TYPES)}")
    if not recipients:
        raise HTTPException(status_code=400, detail="At least one recipient email is required")
    if frequency not in ("daily", "weekly", "monthly"):
        raise HTTPException(status_code=400, detail="Frequency must be daily, weekly, or monthly")

    # Calculate next run
    now = datetime.now(timezone.utc)
    if frequency == "daily":
        next_run = now + timedelta(days=1)
    elif frequency == "weekly":
        next_run = now + timedelta(weeks=1)
    else:
        next_run = now + timedelta(days=30)
    next_run = next_run.replace(hour=8, minute=0, second=0)

    insert_sql = """INSERT INTO report_schedules (name, report_type, frequency, recipients, filters, next_run_at, created_by, is_active)
                       VALUES (:name, :type, :freq, :recip, :filters, :next, :uid, :active)"""
    params = {"name": name, "type": report_type, "freq": frequency,
              "recip": json.dumps(recipients), "filters": json.dumps(body.get("filters", {})),
              "next": next_run, "uid": current_user["id"], "active": body.get("is_active", True)}
    sched_id = execute_insert_returning_id(db, insert_sql, params)
    db.commit()
    return {"id": sched_id, "status": "ok"}


@router.delete("/report-schedules/{schedule_id}")
async def delete_report_schedule(schedule_id: int, current_user: dict = Depends(require_superadmin(scope="admin")), db: Session = Depends(get_db)):
    """Delete a scheduled report. Superadmin only."""
    row = db.execute(text("SELECT 1 FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.execute(text("DELETE FROM report_schedules WHERE id = :id"), {"id": schedule_id})
    db.commit()
    return {"status": "ok"}


@router.patch("/report-schedules/{schedule_id}")
async def update_report_schedule(schedule_id: int, body: dict, current_user: dict = Depends(require_superadmin(scope="admin")), db: Session = Depends(get_db)):
    """Update a scheduled report. Superadmin only."""
    row = db.execute(text("SELECT 1 FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")

    body = _validated_schedule_fields(body, partial=True)
    sets = []
    params = {"id": schedule_id}
    for field in ["name", "report_type", "frequency", "is_active"]:
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]
    if "recipients" in body:
        sets.append("recipients = :recipients")
        params["recipients"] = json.dumps(body["recipients"])
    if "filters" in body:
        sets.append("filters = :filters")
        params["filters"] = json.dumps(body["filters"])
    if sets:
        db.execute(text(f"UPDATE report_schedules SET {', '.join(sets)} WHERE id = :id"), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()

    return {"status": "ok"}


@router.post("/report-schedules/{schedule_id}/run")
async def run_report_now(schedule_id: int, current_user: dict = Depends(require_superadmin(scope="admin")), db: Session = Depends(get_db)):
    """Immediately generate and email a scheduled report. Superadmin only."""
    row = db.execute(text("SELECT * FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    from modules.reporting.report_runner import run_report, ReportDeliveryError
    try:
        run_report(dict(row._mapping))
    except ReportDeliveryError as e:
        raise HTTPException(502, detail={"message":"Report was not delivered to every recipient", "sent_count":e.sent, "failed_count":e.failed})
    except RuntimeError as e:
        log.warning("Report run-now failed for schedule %s (%s)", schedule_id, type(e).__name__)
        raise HTTPException(status_code=400, detail="Report generation failed. Check report configuration.")
    except Exception as e:
        log.error("Run-now report %s failed (%s)", schedule_id, type(e).__name__)
        raise HTTPException(status_code=500, detail="Report generation failed")
    return {"status": "sent"}
