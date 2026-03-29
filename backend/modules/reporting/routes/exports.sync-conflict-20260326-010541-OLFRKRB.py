"""O.D.I.N. — CSV Export and Audit Log endpoints."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timezone
import json
import logging
import csv
import io

from core.db import get_db
from core.rbac import require_role
from core.models import AuditLog
from modules.jobs.models import Job
from modules.inventory.models import Spool, SpoolUsage
from modules.models_library.models import Model

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Export"])


# ============== CSV Export ==============

@router.get("/export/jobs")
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


@router.get("/export/spools")
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


@router.get("/export/filament-usage")
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


@router.get("/export/models")
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


@router.get("/export/audit-logs")
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
