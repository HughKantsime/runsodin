"""O.D.I.N. — Chargeback Reports and Report Schedules."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timedelta, timezone
import json
import logging

from core.db import get_db
from core.rbac import require_role

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Reports"])

REPORT_TYPES = ["fleet_utilization", "job_summary", "filament_consumption", "failure_analysis", "chargeback_summary"]


# ============== Chargeback Report ==============

@router.get("/reports/chargebacks")
async def chargeback_report(
    start_date: str = None, end_date: str = None,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Generate chargeback report — cost summary by user."""
    query = """
        SELECT j.charged_to_user_id as user_id, u.username,
               COUNT(*) as job_count,
               SUM(j.estimated_cost) as total_cost,
               SUM(j.duration_hours) as total_hours
        FROM jobs j
        LEFT JOIN users u ON j.charged_to_user_id = u.id
        WHERE j.charged_to_user_id IS NOT NULL
    """
    params = {}
    if start_date:
        query += " AND j.created_at >= :start"
        params["start"] = start_date
    if end_date:
        query += " AND j.created_at <= :end"
        params["end"] = end_date
    query += " GROUP BY j.charged_to_user_id ORDER BY total_cost DESC"

    rows = db.execute(text(query), params).fetchall()
    return [{
        "user_id": r.user_id, "username": r.username or f"[user-{r.user_id}]",
        "job_count": r.job_count,
        "total_cost": round(r.total_cost or 0, 2),
        "total_hours": round(r.total_hours or 0, 1),
    } for r in rows]


# ============== Report Schedules ==============

@router.get("/report-schedules")
async def list_report_schedules(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """List all scheduled reports."""
    rows = db.execute(text("SELECT * FROM report_schedules ORDER BY created_at DESC")).fetchall()
    return [{
        "id": r.id, "name": r.name, "report_type": r.report_type,
        "frequency": r.frequency, "recipients": json.loads(r.recipients) if r.recipients else [],
        "filters": json.loads(r.filters) if r.filters else {},
        "is_active": bool(r.is_active), "next_run_at": r.next_run_at,
        "last_run_at": r.last_run_at, "created_at": r.created_at,
    } for r in rows]


@router.post("/report-schedules")
async def create_report_schedule(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Create a new scheduled report."""
    name = body.get("name", "").strip()
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

    db.execute(text("""INSERT INTO report_schedules (name, report_type, frequency, recipients, filters, next_run_at, created_by)
                       VALUES (:name, :type, :freq, :recip, :filters, :next, :uid)"""),
               {"name": name, "type": report_type, "freq": frequency,
                "recip": json.dumps(recipients), "filters": json.dumps(body.get("filters", {})),
                "next": next_run, "uid": current_user["id"]})
    db.commit()

    sched_id = db.execute(text("SELECT last_insert_rowid()")).scalar()
    return {"id": sched_id, "status": "ok"}


@router.delete("/report-schedules/{schedule_id}")
async def delete_report_schedule(schedule_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a scheduled report."""
    row = db.execute(text("SELECT 1 FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.execute(text("DELETE FROM report_schedules WHERE id = :id"), {"id": schedule_id})
    db.commit()
    return {"status": "ok"}


@router.patch("/report-schedules/{schedule_id}")
async def update_report_schedule(schedule_id: int, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update a scheduled report (toggle active, change recipients, etc.)."""
    row = db.execute(text("SELECT 1 FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")

    sets = []
    params = {"id": schedule_id}
    for field in ["name", "frequency", "is_active"]:
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]
    if "recipients" in body:
        sets.append("recipients = :recipients")
        params["recipients"] = json.dumps(body["recipients"])
    if sets:
        db.execute(text(f"UPDATE report_schedules SET {', '.join(sets)} WHERE id = :id"), params)
        db.commit()

    return {"status": "ok"}


@router.post("/report-schedules/{schedule_id}/run")
async def run_report_now(schedule_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Immediately generate and email a scheduled report."""
    row = db.execute(text("SELECT * FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    from modules.reporting.report_runner import run_report
    try:
        run_report(dict(row._mapping))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"Run-now report {schedule_id} failed: {e}")
        raise HTTPException(status_code=500, detail="Report generation failed")
    return {"status": "sent"}
