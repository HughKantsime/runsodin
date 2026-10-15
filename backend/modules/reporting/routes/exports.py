"""O.D.I.N. — CSV Export and Audit Log endpoints."""

from core.rbac import should_filter_org

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, or_, cast, String, literal_column
from typing import Optional, Annotated
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
import csv
import re
from pydantic import BeforeValidator
import io

from core.db import get_db
from core.rbac import require_role, require_superadmin, get_org_scope
from core.models import AuditLog
from modules.jobs.models import Job
from modules.inventory.models import Spool, SpoolUsage
from modules.models_library.models import Model

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Export"])


# ============== CSV Export ==============

def _strict_audit_date(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
        raise ValueError('Expected YYYY-MM-DD')
    return date.fromisoformat(value)


AuditDate = Annotated[date, BeforeValidator(_strict_audit_date)]


def _audit_csv_text(value):
    if value is None:
        return ''
    value = str(value)
    return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value

@router.get("/export/jobs")
def export_jobs_csv(
    status: Optional[str] = None,
    current_user: dict = Depends(require_role("operator", scope="read:jobs")),
    db: Session = Depends(get_db)
):
    """Export jobs as CSV."""
    org = get_org_scope(current_user)
    query = db.query(Job)
    if should_filter_org(current_user, org):
        query = query.filter((Job.charged_to_org_id == org) | (Job.charged_to_org_id == None))
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
def export_spools_csv(current_user: dict = Depends(require_role("operator", scope="read:spools")), db: Session = Depends(get_db)):
    """Export spools as CSV."""
    org = get_org_scope(current_user)
    sq = db.query(Spool)
    if should_filter_org(current_user, org):
        sq = sq.filter((Spool.org_id == org) | (Spool.org_id == None))
    spools = sq.order_by(Spool.id).all()

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
def export_filament_usage_csv(current_user: dict = Depends(require_role("operator", scope="read:spools")), db: Session = Depends(get_db)):
    """Export filament usage history as CSV."""
    org = get_org_scope(current_user)
    uq = db.query(SpoolUsage)
    if should_filter_org(current_user, org):
        uq = uq.join(Spool, SpoolUsage.spool_id == Spool.id).filter(
            (Spool.org_id == org) | (Spool.org_id == None)
        )
    usage_records = uq.order_by(SpoolUsage.used_at.desc()).all()

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
def export_models_csv(current_user: dict = Depends(require_role("operator", scope="read:models")), db: Session = Depends(get_db)):
    """Export models as CSV."""
    org = get_org_scope(current_user)
    mq = db.query(Model)
    if should_filter_org(current_user, org):
        mq = mq.filter((Model.org_id == org) | (Model.org_id == None))
    models = mq.order_by(Model.name).all()

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


def _audit_projection(db):
    # Raw additive migration fields deliberately stay outside frozen ORM metadata.
    return db.query(AuditLog, *[
        literal_column('audit_logs.' + name).label(name)
        for name in ('actor_user_id', 'actor_username', 'actor_auth_kind')
    ])


@router.get("/export/audit-logs")
def export_audit_logs_csv(
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    date_from: Optional[AuditDate] = None,
    date_to: Optional[AuditDate] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    current_user: dict = Depends(require_superadmin(scope='read')),
    db: Session = Depends(get_db)
):
    """Export audit logs as CSV. Superadmin only — audit logs span all tenants."""
    query = _audit_filters(_audit_projection(db), entity_type=entity_type, action=action,
                          date_from=date_from, date_to=date_to, q=q)
    logs = query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(5000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Timestamp", "Action", "Entity Type", "Entity ID", "Details", "IP Address", "User ID", "Username", "Authentication"])
    for log_entry, actor_id, actor_name, actor_kind in logs:
        writer.writerow([
            log_entry.id,
            log_entry.timestamp.isoformat() if log_entry.timestamp else "",
            _audit_csv_text(log_entry.action),
            _audit_csv_text(log_entry.entity_type),
            log_entry.entity_id if log_entry.entity_id is not None else "",
            _audit_csv_text(json.dumps(log_entry.details) if isinstance(log_entry.details, dict) else log_entry.details),
            _audit_csv_text(log_entry.ip_address),
            actor_id if actor_id is not None else "",
            _audit_csv_text(actor_name),
            _audit_csv_text(actor_kind),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_logs_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"}
    )


# ============== Audit Log ==============

def _audit_filters(query, *, entity_type=None, action=None, date_from=None, date_to=None, q=None):
    """One bounded literal filter shared by the list and CSV projection."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, 'date_from must not be after date_to')
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(AuditLog.action == action)
    # SQLite CURRENT_TIMESTAMP omits fractions. Comparing that text to a
    # midnight datetime with '.000000' would admit next-day midnight. Date
    # prefixes are exact half-open bounds for its canonical stored dates.
    sqlite_dates = query.session.get_bind().dialect.name == 'sqlite'
    if date_from:
        start = date_from.isoformat() if sqlite_dates else datetime.combine(date_from, time.min)
        query = query.filter(AuditLog.timestamp >= start)
    if date_to:
        if date_to == date.max:
            raise HTTPException(422, 'date_to is outside the supported range')
        next_day = date_to + timedelta(days=1)
        end = next_day.isoformat() if sqlite_dates else datetime.combine(next_day, time.min)
        query = query.filter(AuditLog.timestamp < end)
    if q and q.strip():
        literal = q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = '%' + literal + '%'
        query = query.filter(or_(*[
            column.ilike(pattern, escape='\\') for column in (
                AuditLog.action, AuditLog.entity_type, cast(AuditLog.entity_id, String),
                cast(AuditLog.details, String), AuditLog.ip_address,
                literal_column("audit_logs.actor_username"),
            )
        ]))
    return query

@router.get("/audit-logs", tags=["Audit"])
def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    date_from: Optional[AuditDate] = None,
    date_to: Optional[AuditDate] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_superadmin(scope='read')),
):
    """List audit log entries with pagination and filters. Superadmin only."""
    query = _audit_filters(_audit_projection(db), entity_type=entity_type, action=action,
                          date_from=date_from, date_to=date_to, q=q)

    total = query.count()
    logs = query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "logs": [
            {
                "id": log_entry.id,
                "user_id": actor_id,
                "username": actor_name,
                "auth_kind": actor_kind,
                "timestamp": log_entry.timestamp.isoformat() if log_entry.timestamp else None,
                "action": log_entry.action,
                "entity_type": log_entry.entity_type,
                "entity_id": log_entry.entity_id,
                "details": log_entry.details,
                "ip_address": log_entry.ip_address,
            }
            for log_entry, actor_id, actor_name, actor_kind in logs
        ],
    }
