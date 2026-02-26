"""O.D.I.N. — Analytics, Stats, and Usage Reports."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timedelta, timezone
import logging
import httpx

from core.db import get_db
from core.rbac import require_role
from core.config import settings
from core.base import JobStatus
from modules.printers.models import Printer
from modules.models_library.models import Model
from modules.jobs.models import Job
from license_manager import require_feature

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Analytics"])


# ============== Stats ==============

@router.get("/stats")
async def get_stats(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Get dashboard statistics."""
    total_printers = db.query(Printer).count()
    active_printers = db.query(Printer).filter(Printer.is_active.is_(True)).count()

    pending_jobs = db.query(Job).filter(Job.status == JobStatus.PENDING).count()
    scheduled_jobs = db.query(Job).filter(Job.status == JobStatus.SCHEDULED).count()
    printing_jobs = db.query(Job).filter(Job.status == JobStatus.PRINTING).count()
    completed_today = db.query(Job).filter(
        Job.status == JobStatus.COMPLETED,
        Job.actual_end >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
    ).count()

    # Include MQTT-tracked jobs
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
    mqtt_printing = db.execute(text("SELECT COUNT(*) FROM print_jobs WHERE status = 'running'")).scalar() or 0
    mqtt_completed_today = db.execute(text("SELECT COUNT(*) FROM print_jobs WHERE status = 'completed' AND ended_at >= :today"), {"today": today_start}).scalar() or 0

    printing_jobs += mqtt_printing
    completed_today += mqtt_completed_today

    total_models = db.query(Model).count()

    # Check Spoolman connection
    spoolman_connected = False
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=3)
                spoolman_connected = resp.status_code == 200
        except Exception:
            pass

    # --- Printer utilization stats for Utilization page ---
    printer_stats = []
    all_printers = db.query(Printer).filter(Printer.is_active.is_(True)).all()
    for p in all_printers:
        # Count completed and failed jobs for this printer
        completed_jobs = db.query(Job).filter(
            Job.printer_id == p.id,
            Job.status == JobStatus.COMPLETED
        ).count()
        # Also count MQTT-tracked completed jobs
        mqtt_completed = db.execute(
            text("SELECT COUNT(*) FROM print_jobs WHERE printer_id = :pid AND status = 'completed'"),
            {"pid": p.id}
        ).scalar() or 0
        completed_jobs += mqtt_completed

        failed_jobs = db.query(Job).filter(
            Job.printer_id == p.id,
            Job.status == JobStatus.FAILED
        ).count()
        mqtt_failed = db.execute(
            text("SELECT COUNT(*) FROM print_jobs WHERE printer_id = :pid AND status = 'failed'"),
            {"pid": p.id}
        ).scalar() or 0
        failed_jobs += mqtt_failed

        total_hours = round(p.total_print_hours or 0, 1)
        total_jobs = completed_jobs + failed_jobs
        success_rate = round((completed_jobs / total_jobs * 100), 1) if total_jobs > 0 else 100.0
        avg_job_hours = round(total_hours / completed_jobs, 1) if completed_jobs > 0 else 0

        # Utilization: hours printed / hours available (assume 24h/day over last 30 days = 720h)
        utilization_pct = round(min(total_hours / 720 * 100, 100), 1) if total_hours > 0 else 0

        printer_stats.append({
            "id": p.id,
            "name": p.name,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
            "total_hours": total_hours,
            "utilization_pct": utilization_pct,
            "success_rate": success_rate,
            "avg_job_hours": avg_job_hours,
        })

    return {
        "printers": {
            "total": total_printers,
            "active": active_printers
        },
        "jobs": {
            "pending": pending_jobs,
            "scheduled": scheduled_jobs,
            "printing": printing_jobs,
            "completed_today": completed_today
        },
        "models": total_models,
        "spoolman_connected": spoolman_connected,
        "printer_stats": printer_stats
    }


# ============== Analytics ==============

@router.get("/analytics")
def get_analytics(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Get analytics data for dashboard."""
    from sqlalchemy import func

    # Get all models with profitability data
    models = db.query(Model).all()

    # Top models by value per hour
    models_by_value = sorted(
        [m for m in models if m.cost_per_item and m.build_time_hours],
        key=lambda m: (m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours,
        reverse=True
    )[:10]

    top_by_hour = [{
        "id": m.id,
        "name": m.name,
        "value_per_hour": round((m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours, 2),
        "value_per_bed": round(m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1), 2),
        "build_time_hours": m.build_time_hours,
        "units_per_bed": m.units_per_bed or 1,
    } for m in models_by_value]

    # Worst performers
    models_by_value_asc = sorted(
        [m for m in models if m.cost_per_item and m.build_time_hours],
        key=lambda m: (m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours,
    )[:10]

    worst_performers = [{
        "id": m.id,
        "name": m.name,
        "value_per_hour": round((m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours, 2),
        "value_per_bed": round(m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1), 2),
        "build_time_hours": m.build_time_hours,
    } for m in models_by_value_asc]

    # Jobs stats
    all_jobs = db.query(Job).all()
    completed_jobs = [j for j in all_jobs if j.status == "completed"]
    pending_jobs = [j for j in all_jobs if j.status in ("pending", "scheduled")]

    # Revenue and costs from completed jobs (use job.suggested_price/estimated_cost when available)
    total_revenue = 0
    total_cost = 0
    total_print_hours = 0
    jobs_with_cost_data = 0

    for job in completed_jobs:
        # Use job's stored cost data if available
        if job.suggested_price:
            total_revenue += job.suggested_price * job.quantity
            jobs_with_cost_data += 1
        elif job.model_id:
            # Fallback to model data for older jobs
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                total_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity

        if job.estimated_cost:
            total_cost += job.estimated_cost * job.quantity

        if job.duration_hours:
            total_print_hours += job.duration_hours

    # Calculate margin
    total_margin = total_revenue - total_cost if total_cost > 0 else 0
    margin_percent = (total_margin / total_revenue * 100) if total_revenue > 0 else 0

    # Projected revenue from pending jobs
    projected_revenue = 0
    projected_cost = 0
    for job in pending_jobs:
        if job.suggested_price:
            projected_revenue += job.suggested_price * job.quantity
        elif job.model_id:
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                projected_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity

        if job.estimated_cost:
            projected_cost += job.estimated_cost * job.quantity

    # Printer utilization
    printers = db.query(Printer).filter(Printer.is_active.is_(True)).all()
    printer_stats = []
    # Calculate time window for utilization (since first completed job or 30 days)
    now = datetime.now(timezone.utc)
    for printer in printers:
        printer_jobs = [j for j in completed_jobs if j.printer_id == printer.id]
        hours = sum(j.duration_hours or 0 for j in printer_jobs)
        # Utilization = print hours / available hours (since printer's first job, max 30 days)
        if printer_jobs:
            earliest = min(j.created_at for j in printer_jobs if j.created_at)
            if earliest.tzinfo is None:
                earliest = earliest.replace(tzinfo=timezone.utc)
            available_hours = min((now - earliest).total_seconds() / 3600, 30 * 24)
            utilization_pct = round((hours / available_hours * 100), 1) if available_hours > 0 else 0
        else:
            available_hours = 0
            utilization_pct = 0
        # Average job duration
        avg_hours = round(hours / len(printer_jobs), 1) if printer_jobs else 0
        # Success rate
        total_printer_jobs = [j for j in db.query(Job).filter(Job.printer_id == printer.id).all()]
        failed = len([j for j in total_printer_jobs if j.status == 'failed'])
        total_attempted = len([j for j in total_printer_jobs if j.status in ('complete', 'failed')])
        success_rate = round(((total_attempted - failed) / total_attempted * 100), 1) if total_attempted > 0 else 100
        printer_stats.append({
            "id": printer.id,
            "name": printer.name,
            "completed_jobs": len(printer_jobs),
            "total_hours": round(hours, 1),
            "utilization_pct": utilization_pct,
            "avg_job_hours": avg_hours,
            "success_rate": success_rate,
            "failed_jobs": failed,
            "has_plug": bool(getattr(printer, 'plug_type', None)),
        })

    # Jobs over time (last 30 days)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    recent_jobs = db.query(Job).filter(Job.created_at >= thirty_days_ago).all()

    # Group by date
    jobs_by_date = {}
    for job in recent_jobs:
        date_str = job.created_at.strftime("%Y-%m-%d")
        if date_str not in jobs_by_date:
            jobs_by_date[date_str] = {"created": 0, "completed": 0}
        jobs_by_date[date_str]["created"] += 1
        if job.status == "completed":
            jobs_by_date[date_str]["completed"] += 1

    # Average $/hour across all models
    valid_models = [m for m in models if m.cost_per_item and m.build_time_hours]
    if valid_models:
        avg_value_per_hour = sum(
            (m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours
            for m in valid_models
        ) / len(valid_models)
    else:
        avg_value_per_hour = 0

    return {
        "top_by_hour": top_by_hour,
        "worst_performers": worst_performers,
        "summary": {
            "total_models": len(models),
            "total_jobs": len(all_jobs),
            "completed_jobs": len(completed_jobs),
            "pending_jobs": len(pending_jobs),
            "total_revenue": round(total_revenue, 2),
            "total_cost": round(total_cost, 2),
            "total_margin": round(total_margin, 2),
            "margin_percent": round(margin_percent, 1),
            "projected_revenue": round(projected_revenue, 2),
            "projected_cost": round(projected_cost, 2),
            "total_print_hours": round(total_print_hours, 1),
            "avg_value_per_hour": round(avg_value_per_hour, 2),
            "jobs_with_cost_data": jobs_with_cost_data,
        },
        "printer_stats": printer_stats,
        "jobs_by_date": jobs_by_date,
    }


# ============== Failure Analytics ==============

@router.get("/analytics/failures")
def get_failure_analytics(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer")),
):
    """Fleet failure analytics — rates by printer, model, filament, common reasons, HMS errors."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # All completed + failed jobs in window
    jobs = (
        db.query(Job)
        .filter(Job.status.in_(["completed", "failed"]), Job.created_at >= cutoff)
        .all()
    )

    # --- By printer ---
    by_printer = {}
    for j in jobs:
        if not j.printer_id:
            continue
        p = by_printer.setdefault(j.printer_id, {"name": j.printer.name if j.printer else str(j.printer_id), "completed": 0, "failed": 0, "fail_timestamps": []})
        if j.status.value == "completed":
            p["completed"] += 1
        else:
            p["failed"] += 1
            if j.actual_end:
                p["fail_timestamps"].append(j.actual_end)

    printer_stats = []
    for pid, p in by_printer.items():
        total = p["completed"] + p["failed"]
        success_rate = round(p["completed"] / total * 100, 1) if total else 0
        # MTBF: average time between failures
        mtbf_hours = None
        if len(p["fail_timestamps"]) >= 2:
            ts = sorted(p["fail_timestamps"])
            gaps = [(ts[i+1] - ts[i]).total_seconds() / 3600 for i in range(len(ts)-1)]
            mtbf_hours = round(sum(gaps) / len(gaps), 1)
        printer_stats.append({
            "name": p["name"],
            "completed": p["completed"],
            "failed": p["failed"],
            "success_rate": success_rate,
            "mtbf_hours": mtbf_hours,
        })

    # --- By model ---
    by_model = {}
    for j in jobs:
        name = j.model.name if j.model else j.item_name
        m = by_model.setdefault(name, {"completed": 0, "failed": 0})
        if j.status.value == "completed":
            m["completed"] += 1
        else:
            m["failed"] += 1

    model_stats = [
        {"name": k, "completed": v["completed"], "failed": v["failed"],
         "success_rate": round(v["completed"] / (v["completed"] + v["failed"]) * 100, 1)}
        for k, v in by_model.items() if v["completed"] + v["failed"] >= 2
    ]
    model_stats.sort(key=lambda x: x["success_rate"])

    # --- By filament type ---
    by_filament = {}
    for j in jobs:
        ft = j.filament_type.value if j.filament_type else "unknown"
        f = by_filament.setdefault(ft, {"completed": 0, "failed": 0})
        if j.status.value == "completed":
            f["completed"] += 1
        else:
            f["failed"] += 1

    filament_stats = [
        {"type": k, "completed": v["completed"], "failed": v["failed"],
         "failure_rate": round(v["failed"] / (v["completed"] + v["failed"]) * 100, 1)}
        for k, v in by_filament.items() if v["completed"] + v["failed"] >= 1
    ]

    # --- Failure reasons ---
    failed_jobs = [j for j in jobs if j.status.value == "failed"]
    reason_counts = {}
    for j in failed_jobs:
        r = j.fail_reason or "unspecified"
        reason_counts[r] = reason_counts.get(r, 0) + 1
    top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:10]

    # --- HMS error frequency ---
    hms_rows = db.execute(
        text("SELECT code, message, COUNT(*) as cnt FROM hms_error_history WHERE occurred_at >= :cutoff GROUP BY code ORDER BY cnt DESC LIMIT 10"),
        {"cutoff": cutoff.isoformat()},
    ).fetchall()
    hms_errors = [{"code": r[0], "message": r[1], "count": r[2]} for r in hms_rows]

    total_completed = sum(1 for j in jobs if j.status.value == "completed")
    total_failed = len(failed_jobs)

    return {
        "total_completed": total_completed,
        "total_failed": total_failed,
        "overall_success_rate": round(total_completed / (total_completed + total_failed) * 100, 1) if (total_completed + total_failed) else 0,
        "by_printer": printer_stats,
        "by_model": model_stats[:15],
        "by_filament": filament_stats,
        "top_failure_reasons": [{"reason": r, "count": c} for r, c in top_reasons],
        "hms_errors": hms_errors,
    }


# ============== Time Accuracy Analytics ==============

@router.get("/analytics/time-accuracy")
def get_time_accuracy(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer")),
):
    """Estimated vs actual print time accuracy stats."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    completed = (
        db.query(Job)
        .filter(
            Job.status == "completed",
            Job.actual_start.isnot(None),
            Job.actual_end.isnot(None),
            Job.duration_hours.isnot(None),
            Job.duration_hours > 0,
            Job.actual_end >= cutoff,
        )
        .all()
    )

    per_printer = {}
    per_model = {}
    records = []

    for job in completed:
        actual_sec = (job.actual_end - job.actual_start).total_seconds()
        if actual_sec <= 0:
            continue
        actual_h = actual_sec / 3600
        est_h = float(job.duration_hours)
        accuracy = min(est_h, actual_h) / max(est_h, actual_h) * 100

        records.append({
            "job_id": job.id,
            "date": job.actual_end.strftime("%Y-%m-%d"),
            "estimated_hours": round(est_h, 2),
            "actual_hours": round(actual_h, 2),
            "accuracy_pct": round(accuracy, 1),
        })

        # Aggregate by printer
        if job.printer_id:
            p = per_printer.setdefault(job.printer_id, {"name": job.printer.name if job.printer else str(job.printer_id), "est": 0, "act": 0, "count": 0})
            p["est"] += est_h
            p["act"] += actual_h
            p["count"] += 1

        # Aggregate by model
        if job.model_id:
            m = per_model.setdefault(job.model_id, {"name": job.model.name if job.model else str(job.model_id), "est": 0, "act": 0, "count": 0})
            m["est"] += est_h
            m["act"] += actual_h
            m["count"] += 1

    def summarize(agg):
        return [
            {
                "name": v["name"],
                "estimated_hours": round(v["est"], 1),
                "actual_hours": round(v["act"], 1),
                "count": v["count"],
                "accuracy_pct": round(min(v["est"], v["act"]) / max(v["est"], v["act"]) * 100, 1) if max(v["est"], v["act"]) > 0 else 0,
            }
            for v in agg.values()
        ]

    avg_accuracy = round(sum(r["accuracy_pct"] for r in records) / len(records), 1) if records else 0

    return {
        "total_jobs": len(records),
        "avg_accuracy_pct": avg_accuracy,
        "by_printer": summarize(per_printer),
        "by_model": summarize(per_model),
        "recent": records[-50:],
    }


# ============== Education Usage Report ==============

@router.get("/education/usage-report", tags=["Education"])
def get_education_usage_report(
    days: int = Query(default=30, ge=7, le=90),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Education usage report — per-user job metrics and summary stats."""
    require_feature("usage_reports")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Get all users — non-admin callers can only see their own org
    if current_user.get("role") == "admin":
        users_rows = db.execute(
            text("SELECT id, username, email, role, is_active, last_login FROM users")
        ).fetchall()
    else:
        user_group_id = current_user.get("group_id")
        if user_group_id is None:
            users_rows = []
        else:
            users_rows = db.execute(
                text("SELECT id, username, email, role, is_active, last_login FROM users WHERE group_id = :gid"),
                {"gid": user_group_id}
            ).fetchall()

    # Get jobs in window, eager-load model for filament data
    jobs_in_range = (
        db.query(Job)
        .options(joinedload(Job.model))
        .filter(Job.created_at >= cutoff)
        .all()
    )

    # Build per-user stats
    user_stats = []
    fleet_hours = 0
    fleet_jobs = 0
    fleet_approved = 0
    fleet_rejected = 0
    active_ids = set()

    for row in users_rows:
        u = dict(row._mapping)
        uid = u["id"]
        user_jobs = [j for j in jobs_in_range if j.submitted_by == uid]
        if not user_jobs:
            continue

        active_ids.add(uid)
        n_submitted = len(user_jobs)
        n_approved = sum(1 for j in user_jobs if j.approved_by is not None and j.rejected_reason is None)
        n_rejected = sum(1 for j in user_jobs if j.rejected_reason is not None)
        n_completed = sum(1 for j in user_jobs if j.status == JobStatus.COMPLETED)
        n_failed = sum(1 for j in user_jobs if j.status == JobStatus.FAILED)

        hours = sum(
            (j.duration_hours or (j.model.build_time_hours if j.model else 0) or 0) * j.quantity
            for j in user_jobs if j.status == JobStatus.COMPLETED
        )
        grams = sum(
            (j.model.total_filament_grams if j.model else 0) * j.quantity
            for j in user_jobs if j.status == JobStatus.COMPLETED
        )

        last_act = max((j.created_at for j in user_jobs), default=None)

        user_stats.append({
            "user_id": uid,
            "username": u["username"],
            "email": u["email"],
            "role": u["role"],
            "total_jobs_submitted": n_submitted,
            "total_jobs_approved": n_approved,
            "total_jobs_rejected": n_rejected,
            "total_jobs_completed": n_completed,
            "total_jobs_failed": n_failed,
            "total_print_hours": round(hours, 1),
            "total_filament_grams": round(grams, 1),
            "approval_rate": round(n_approved / n_submitted * 100, 1) if n_submitted else 0,
            "success_rate": round(n_completed / (n_completed + n_failed) * 100, 1) if (n_completed + n_failed) else 0,
            "last_activity": last_act.isoformat() if last_act else None,
        })

        fleet_hours += hours
        fleet_jobs += n_submitted
        fleet_approved += n_approved
        fleet_rejected += n_rejected

    user_stats.sort(key=lambda x: x["total_jobs_submitted"], reverse=True)

    # Daily submissions for chart
    daily = {}
    for j in jobs_in_range:
        d = j.created_at.strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0) + 1

    return {
        "summary": {
            "total_users_active": len(active_ids),
            "total_print_hours": round(fleet_hours, 1),
            "total_jobs": fleet_jobs,
            "approval_rate": round(fleet_approved / fleet_jobs * 100, 1) if fleet_jobs else 0,
            "rejection_rate": round(fleet_rejected / fleet_jobs * 100, 1) if fleet_jobs else 0,
        },
        "users": user_stats,
        "daily_submissions": daily,
        "days": days,
    }
