"""O.D.I.N. — Analytics, Stats, Export & Report Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import json
import logging
import csv
import io
import httpx

from deps import get_db, get_current_user, require_role, log_audit, _get_org_filter
from models import (
    Printer, Model, Job, JobStatus, Spool, SpoolUsage, AuditLog,
    SystemConfig, FilamentLibrary,
)
from config import settings
from license_manager import require_feature

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Stats ==============

@router.get("/stats", tags=["Stats"])
async def get_stats(db: Session = Depends(get_db)):
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

@router.get("/analytics", tags=["Analytics"])
def get_analytics(db: Session = Depends(get_db)):
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

@router.get("/analytics/failures", tags=["Analytics"])
def get_failure_analytics(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
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

@router.get("/analytics/time-accuracy", tags=["Analytics"])
def get_time_accuracy(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """Estimated vs actual print time accuracy stats."""
    from sqlalchemy import func as fn

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

    # Get all users
    users_rows = db.execute(
        text("SELECT id, username, email, role, is_active, last_login FROM users")
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


# ============== CSV Export ==============

@router.get("/export/jobs", tags=["Export"])
def export_jobs_csv(
    status: Optional[str] = None,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Export jobs as CSV."""
    query = db.query(Job)
    if status:
        query = query.filter(Job.status == status)
    jobs = query.order_by(Job.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "ID", "Item Name", "Model ID", "Quantity", "Status", "Priority",
        "Printer ID", "Duration (hrs)", "Estimated Cost", "Suggested Price",
        "Scheduled Start", "Actual Start", "Actual End", "Created At"
    ])

    # Data
    for job in jobs:
        writer.writerow([
            job.id,
            job.item_name,
            job.model_id,
            job.quantity,
            job.status.value if job.status else "",
            job.priority,
            job.printer_id,
            job.duration_hours,
            job.estimated_cost,
            job.suggested_price,
            job.scheduled_start.isoformat() if job.scheduled_start else "",
            job.actual_start.isoformat() if job.actual_start else "",
            job.actual_end.isoformat() if job.actual_end else "",
            job.created_at.isoformat() if job.created_at else ""
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs_export.csv"}
    )


@router.get("/export/spools", tags=["Export"])
def export_spools_csv(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Export spools as CSV."""
    spools = db.query(Spool).order_by(Spool.id).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "ID", "Filament ID", "QR Code", "RFID Tag", "Color Hex",
        "Initial Weight (g)", "Remaining Weight (g)", "Status",
        "Printer ID", "Slot", "Storage Location", "Vendor", "Price", "Created At"
    ])

    # Data
    for spool in spools:
        writer.writerow([
            spool.id,
            spool.filament_id,
            spool.qr_code,
            spool.rfid_tag,
            spool.color_hex,
            spool.initial_weight_g,
            spool.remaining_weight_g,
            spool.status.value if spool.status else "",
            spool.location_printer_id,
            spool.location_slot,
            spool.storage_location,
            spool.vendor,
            spool.price,
            spool.created_at.isoformat() if spool.created_at else ""
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spools_export.csv"}
    )


@router.get("/export/filament-usage", tags=["Export"])
def export_filament_usage_csv(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Export filament usage history as CSV."""
    usage_records = db.query(SpoolUsage).order_by(SpoolUsage.used_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "ID", "Spool ID", "Job ID", "Weight Used (g)", "Used At", "Notes"
    ])

    # Data
    for usage in usage_records:
        writer.writerow([
            usage.id,
            usage.spool_id,
            usage.job_id,
            usage.weight_used_g,
            usage.used_at.isoformat() if usage.used_at else "",
            usage.notes
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=filament_usage_export.csv"}
    )


@router.get("/export/models", tags=["Export"])
def export_models_csv(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Export models as CSV."""
    models = db.query(Model).order_by(Model.name).all()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        "ID", "Name", "Category", "Filament Type", "Build Time (hrs)",
        "Total Filament (g)", "Cost Per Item", "Markup %", "Units Per Bed", "Created At"
    ])

    # Data
    for model in models:
        writer.writerow([
            model.id,
            model.name,
            model.category,
            model.default_filament_type.value if model.default_filament_type else "",
            model.build_time_hours,
            model.total_filament_grams,
            model.cost_per_item,
            model.markup_percent,
            model.units_per_bed,
            model.created_at.isoformat() if model.created_at else ""
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=models_export.csv"}
    )


@router.get("/export/audit-logs", tags=["Export"])
def export_audit_logs_csv(
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Export audit logs as CSV."""
    query = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(AuditLog.action == action)
    logs = query.limit(5000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Timestamp", "Action", "Entity Type", "Entity ID", "Details", "IP Address"])
    for log_entry in logs:
        writer.writerow([
            log_entry.id,
            log_entry.timestamp.isoformat() if log_entry.timestamp else "",
            log_entry.action,
            log_entry.entity_type or "",
            log_entry.entity_id or "",
            json.dumps(log_entry.details) if isinstance(log_entry.details, dict) else (log_entry.details or ""),
            log_entry.ip_address or ""
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_logs_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"}
    )


# ============== Audit Log ==============

@router.get("/audit-logs", tags=["Audit"])
def list_audit_logs(
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("admin")),
):
    """List audit log entries with pagination and filters."""
    query = db.query(AuditLog).order_by(AuditLog.timestamp.desc())

    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(AuditLog.action == action)
    if date_from:
        query = query.filter(AuditLog.timestamp >= date_from)
    if date_to:
        query = query.filter(AuditLog.timestamp <= date_to + "T23:59:59")

    total = query.count()
    logs = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "logs": [
            {
                "id": log_entry.id,
                "timestamp": log_entry.timestamp.isoformat() if log_entry.timestamp else None,
                "action": log_entry.action,
                "entity_type": log_entry.entity_type,
                "entity_id": log_entry.entity_id,
                "details": log_entry.details,
                "ip_address": log_entry.ip_address,
            }
            for log_entry in logs
        ],
    }


# ============== Chargeback Report ==============

@router.get("/reports/chargebacks", tags=["Reports"])
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

REPORT_TYPES = ["fleet_utilization", "job_summary", "filament_consumption", "failure_analysis", "chargeback_summary"]


@router.get("/report-schedules", tags=["Reports"])
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


@router.post("/report-schedules", tags=["Reports"])
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


@router.delete("/report-schedules/{schedule_id}", tags=["Reports"])
async def delete_report_schedule(schedule_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a scheduled report."""
    row = db.execute(text("SELECT 1 FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    db.execute(text("DELETE FROM report_schedules WHERE id = :id"), {"id": schedule_id})
    db.commit()
    return {"status": "ok"}


@router.patch("/report-schedules/{schedule_id}", tags=["Reports"])
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


@router.post("/report-schedules/{schedule_id}/run", tags=["Reports"])
async def run_report_now(schedule_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Immediately generate and email a scheduled report."""
    row = db.execute(text("SELECT * FROM report_schedules WHERE id = :id"), {"id": schedule_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    from report_runner import run_report
    try:
        run_report(dict(row._mapping))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"Run-now report {schedule_id} failed: {e}")
        raise HTTPException(status_code=500, detail="Report generation failed")
    return {"status": "sent"}
