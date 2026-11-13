"""System maintenance routes — maintenance task templates, logs, status, and seed defaults."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel as PydanticBaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import (AGENT_READ_SCOPE, AGENT_WRITE_SCOPE, require_any_scope, require_role, require_action,
                       check_org_access)
from core.responses import build_next_actions, next_action
from modules.printers.models import Printer
from modules.system.maintenance_status import compute_status, visible_printer_query
from modules.system.models import MaintenanceTask, MaintenanceLog

log = logging.getLogger("odin.api")
router = APIRouter()


def _visible_printer_query(query, user):
    return visible_printer_query(query, user)


def _authorized_printer(db, printer_id, user):
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(user, printer.org_id) or printer.shared):
        raise OdinError(ErrorCode.printer_not_found, "Printer not found", status=404)
    return printer


# ============== Pydantic models ==============

class MaintenanceTaskCreate(PydanticBaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    printer_model_filter: Optional[str] = Field(default=None, max_length=100)
    interval_print_hours: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    interval_days: Optional[int] = Field(default=None, gt=0)
    estimated_cost: float = Field(default=0, ge=0, allow_inf_nan=False)
    estimated_downtime_min: int = Field(default=30, ge=0)


    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if value is None or not value.strip():
            raise ValueError("Name must not be empty")
        return value


class MaintenanceTaskUpdate(PydanticBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    printer_model_filter: Optional[str] = Field(default=None, max_length=100)
    interval_print_hours: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    interval_days: Optional[int] = Field(default=None, gt=0)
    estimated_cost: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_downtime_min: Optional[int] = Field(default=None, ge=0)
    is_active: Optional[bool] = None


    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if value is None or not value.strip():
            raise ValueError("Name must not be empty")
        return value


class MaintenanceLogCreate(PydanticBaseModel):
    printer_id: int
    task_id: Optional[int] = None
    task_name: str = Field(min_length=1, max_length=200)
    performed_by: Optional[str] = Field(default=None, max_length=100)
    notes: Optional[str] = None
    cost: float = Field(default=0, ge=0, allow_inf_nan=False)
    downtime_minutes: int = Field(default=0, ge=0)
    # Optional back-dated service time. Omitted = now. Not in the future, not before 2020-01-01.
    performed_at: Optional[datetime] = None

    @field_validator("performed_at")
    @classmethod
    def valid_performed_at(cls, value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if value > datetime.now(timezone.utc):
            raise ValueError("Service date cannot be in the future")
        if value < datetime(2020, 1, 1, tzinfo=timezone.utc):
            raise ValueError("Service date cannot be before 2020-01-01")
        # Stored like the server default: naive UTC.
        return value.replace(tzinfo=None)


    @field_validator("task_name")
    @classmethod
    def valid_name(cls, value):
        if value is None or not value.strip():
            raise ValueError("Name must not be empty")
        return value


# ============== Task templates ==============

@router.get("/maintenance/tasks", tags=["Maintenance"])
def list_maintenance_tasks(
    db: Session = Depends(get_db),
    # Stacked auth (Phase 2 canonical read shape).
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE)
    ),
):
    """List all maintenance task templates."""
    tasks = db.query(MaintenanceTask).order_by(MaintenanceTask.name).all()
    return [{
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "printer_model_filter": t.printer_model_filter,
        "interval_print_hours": t.interval_print_hours,
        "interval_days": t.interval_days,
        "estimated_cost": t.estimated_cost,
        "estimated_downtime_min": t.estimated_downtime_min,
        "is_active": t.is_active,
    } for t in tasks]


def _printed_since(db, printer_id, since):
    """(hours, prints) this printer completed since `since`.

    Only completed prints count, because only those are added to the printer's odometer
    (printer_health.increment_care_counters); failed, cancelled and retired rows never were.
    """
    from sqlalchemy import text
    floor = since.astimezone(timezone.utc).replace(tzinfo=None) if since.tzinfo else since
    rows = db.execute(text(
        "SELECT started_at, ended_at FROM print_jobs WHERE printer_id = :p AND status = 'completed' "
        "AND started_at IS NOT NULL AND ended_at IS NOT NULL AND ended_at > :since"),
        {"p": printer_id, "since": floor}).all()
    hours, prints = 0.0, 0
    for started, ended in rows:
        try:
            if isinstance(started, str): started = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if isinstance(ended, str): ended = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            started = started.astimezone(timezone.utc).replace(tzinfo=None) if started.tzinfo else started
            ended = ended.astimezone(timezone.utc).replace(tzinfo=None) if ended.tzinfo else ended
            begin = max(started, floor)
            if ended > begin:
                hours += (ended - begin).total_seconds() / 3600.0
                prints += 1
        except (ValueError, TypeError, AttributeError):
            continue
    return hours, prints


@router.post("/maintenance/tasks", tags=["Maintenance"])
def create_maintenance_task(data: MaintenanceTaskCreate, current_user: dict = Depends(require_action("maintenance.tasks", scope="write")), db: Session = Depends(get_db)):
    """Create a new maintenance task template."""
    task = MaintenanceTask(
        name=data.name,
        description=data.description,
        printer_model_filter=data.printer_model_filter,
        interval_print_hours=data.interval_print_hours,
        interval_days=data.interval_days,
        estimated_cost=data.estimated_cost,
        estimated_downtime_min=data.estimated_downtime_min,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id, "name": task.name, "message": "Task created"}


@router.patch("/maintenance/tasks/{task_id}", tags=["Maintenance"])
def update_maintenance_task(task_id: int, data: MaintenanceTaskUpdate, current_user: dict = Depends(require_action("maintenance.tasks", scope="write")), db: Session = Depends(get_db)):
    """Update a maintenance task template."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return {"id": task.id, "message": "Task updated"}


@router.delete("/maintenance/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_task(task_id: int, current_user: dict = Depends(require_role("admin", scope="write")), db: Session = Depends(get_db)):
    """Delete a task template while preserving named service history."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()


# ============== Maintenance logs ==============

@router.get("/maintenance/logs", tags=["Maintenance"])
def list_maintenance_logs(
    printer_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer")),
    _scope: dict = Depends(require_any_scope("read", AGENT_READ_SCOPE, AGENT_WRITE_SCOPE)),
):
    """List maintenance logs, optionally filtered by printer."""
    query = _visible_printer_query(
        db.query(MaintenanceLog).join(Printer, Printer.id == MaintenanceLog.printer_id), current_user
    ).order_by(MaintenanceLog.performed_at.desc())
    if printer_id:
        query = query.filter(MaintenanceLog.printer_id == printer_id)
    logs = query.limit(limit).all()
    return [{
        "id": l.id,
        "printer_id": l.printer_id,
        "task_id": l.task_id,
        "task_name": l.task_name,
        "performed_at": l.performed_at.isoformat() if l.performed_at else None,
        "performed_by": l.performed_by,
        "notes": l.notes,
        "cost": l.cost,
        "downtime_minutes": l.downtime_minutes,
        "print_hours_at_service": l.print_hours_at_service,
    } for l in logs]


@router.post("/maintenance/logs", tags=["Maintenance"])
def create_maintenance_log(
    data: MaintenanceLogCreate,
    request: Request,
    # Stacked auth (Phase 2 canonical) — see routes_controls.py::pause_printer.
    current_user: dict = Depends(require_action("maintenance.log")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Log a maintenance action performed on a printer.

    Agent-surface (v1.9.0 Phase 2): honors X-Dry-Run, emits next_actions.
    """
    printer = _authorized_printer(db, data.printer_id, current_user)
    if data.task_id is not None and not db.query(MaintenanceTask).filter(MaintenanceTask.id == data.task_id).first():
        raise HTTPException(404, "Task not found")
    # Snapshot the same lifetime odometer used by the status calculation.
    total_hours = max(0.0, float(printer.total_print_hours or 0))
    if data.performed_at is not None:
        # A back-dated service happened at an earlier odometer reading: take off the
        # hours printed since then, so hour-based tasks don't restart from today.
        since_hours, since_prints = _printed_since(db, printer.id, data.performed_at)
        total_hours = max(0.0, total_hours - since_hours)

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "complete_maintenance",
                "printer_id": data.printer_id,
                "printer_name": printer.name,
                "task_id": data.task_id,
                "task_name": data.task_name,
                "notes": data.notes,
                "cost": data.cost,
                "downtime_minutes": data.downtime_minutes,
                "print_hours_at_service": total_hours,
                "performed_at": data.performed_at.isoformat() if data.performed_at else None,
            },
            next_actions=[
                next_action(
                    "list_maintenance_tasks",
                    {"overdue_only": True},
                    "verify task no longer overdue",
                ),
            ],
            notes=("Would insert a MaintenanceLog row and reset the printer's care counters "
                   "(prints/hours since maintenance) to 0; nothing is written on dry-run."),
        )

    log_entry = MaintenanceLog(
        printer_id=data.printer_id,
        task_id=data.task_id,
        task_name=data.task_name,
        performed_by=data.performed_by,
        notes=data.notes,
        cost=data.cost,
        downtime_minutes=data.downtime_minutes,
        print_hours_at_service=total_hours,
        **({"performed_at": data.performed_at} if data.performed_at else {}),
    )
    db.add(log_entry)
    # Completing maintenance resets the printer's care counters in the same
    # transaction as the log (the task's own next-due point is recomputed from
    # this log's print_hours_at_service / performed_at by the status endpoint).
    # A back-dated entry that is now the printer's newest service sets the counters to
    # what has been printed since that date; an older one leaves them alone.
    if data.performed_at is None:
        printer.hours_since_maintenance = 0
        printer.prints_since_maintenance = 0
    else:
        newest = db.query(MaintenanceLog.performed_at).filter(
            MaintenanceLog.printer_id == printer.id).order_by(MaintenanceLog.performed_at.desc()).first()
        mine = data.performed_at.astimezone(timezone.utc).replace(tzinfo=None) if data.performed_at.tzinfo else data.performed_at
        if newest is None or newest[0] is None or newest[0].replace(tzinfo=None) <= mine:
            printer.hours_since_maintenance = round(since_hours, 2)
            printer.prints_since_maintenance = since_prints
    db.commit()
    db.refresh(log_entry)
    return {
        "id": log_entry.id,
        "message": "Maintenance logged",
        "print_hours_at_service": total_hours,
        "next_actions": build_next_actions(
            next_action(
                "list_maintenance_tasks",
                {"overdue_only": True},
                "confirm task no longer overdue",
            ),
        ),
    }


@router.delete("/maintenance/logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_log(log_id: int, current_user: dict = Depends(require_role("admin", scope="write")), db: Session = Depends(get_db)):
    """Delete a maintenance log entry."""
    log_entry = db.query(MaintenanceLog).filter(MaintenanceLog.id == log_id).first()
    if not log_entry:
        raise HTTPException(status_code=404, detail="Log not found")
    _authorized_printer(db, log_entry.printer_id, current_user)
    db.delete(log_entry)
    db.commit()


# ============== Maintenance status ==============

@router.get("/maintenance/status", tags=["Maintenance"])
def get_maintenance_status(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer")), _scope: dict = Depends(require_any_scope("read", AGENT_READ_SCOPE, AGENT_WRITE_SCOPE))):
    """Get maintenance status for all active printers. Returns per-printer task health.

    The computation lives in modules/system/maintenance_status.py (shared with the hourly
    notifier). Each printer also carries `failure_rate` (30/90 days); a printer with no
    maintenance task shows the implicit "General service" task (`is_default: true`).
    """
    return compute_status(db, current_user)


# ============== Seed defaults ==============

@router.post("/maintenance/seed-defaults", tags=["Maintenance"])
def seed_default_maintenance_tasks(current_user: dict = Depends(require_role("admin", scope="write")), db: Session = Depends(get_db)):
    """Seed default maintenance tasks for common Bambu Lab printer models."""
    defaults = [
        {"name": "General Cleaning", "description": "Clean build plate, wipe exterior, clear debris from print area",
         "printer_model_filter": None, "interval_print_hours": 50, "interval_days": 14,
         "estimated_cost": 0, "estimated_downtime_min": 15},
        {"name": "Nozzle Inspection", "description": "Check nozzle for wear, clogs, or damage — replace if needed",
         "printer_model_filter": None, "interval_print_hours": 500, "interval_days": None,
         "estimated_cost": 8, "estimated_downtime_min": 15},
        {"name": "Build Plate Check", "description": "Inspect build plate surface — clean, re-level, or replace if worn",
         "printer_model_filter": None, "interval_print_hours": 1000, "interval_days": 180,
         "estimated_cost": 30, "estimated_downtime_min": 10},
        {"name": "Belt Tension Check", "description": "Verify X/Y belt tension and adjust if loose",
         "printer_model_filter": None, "interval_print_hours": 500, "interval_days": None,
         "estimated_cost": 0, "estimated_downtime_min": 20},
        {"name": "Firmware Update Check", "description": "Check for and apply firmware updates",
         "printer_model_filter": None, "interval_print_hours": None, "interval_days": 30,
         "estimated_cost": 0, "estimated_downtime_min": 15},
        {"name": "Carbon Rod Lubrication", "description": "Lubricate carbon rods on X/Y axes (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 5, "estimated_downtime_min": 20},
        {"name": "HEPA Filter Replacement", "description": "Replace HEPA filter in enclosure (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 500, "interval_days": 90,
         "estimated_cost": 12, "estimated_downtime_min": 5},
        {"name": "Purge Wiper Replacement", "description": "Replace purge/wiper assembly (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 6, "estimated_downtime_min": 10},
        {"name": "HEPA Filter Replacement", "description": "Replace HEPA filter in enclosure (P1S)",
         "printer_model_filter": "P1S", "interval_print_hours": 500, "interval_days": 90,
         "estimated_cost": 12, "estimated_downtime_min": 5},
        {"name": "Carbon Rod Lubrication", "description": "Lubricate carbon rods on X/Y axes (P1S)",
         "printer_model_filter": "P1S", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 5, "estimated_downtime_min": 20},
        {"name": "Hotend Cleaning", "description": "Clean hotend assembly and check for leaks (A1 series)",
         "printer_model_filter": "A1", "interval_print_hours": 300, "interval_days": None,
         "estimated_cost": 0, "estimated_downtime_min": 20},
    ]

    created = 0
    skipped = 0
    for d in defaults:
        existing = db.query(MaintenanceTask).filter(
            MaintenanceTask.name == d["name"],
            MaintenanceTask.printer_model_filter == d["printer_model_filter"]
        ).first()
        if not existing:
            task = MaintenanceTask(**d)
            db.add(task)
            created += 1
        else:
            skipped += 1

    db.commit()
    return {"message": f"Seeded {created} tasks ({skipped} already existed)", "created": created, "skipped": skipped}
