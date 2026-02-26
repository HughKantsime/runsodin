"""System maintenance routes — maintenance task templates, logs, status, and seed defaults."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from modules.printers.models import Printer
from modules.system.models import MaintenanceTask, MaintenanceLog

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Pydantic models ==============

class MaintenanceTaskCreate(PydanticBaseModel):
    name: str
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: float = 0
    estimated_downtime_min: int = 30


class MaintenanceTaskUpdate(PydanticBaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: Optional[float] = None
    estimated_downtime_min: Optional[int] = None
    is_active: Optional[bool] = None


class MaintenanceLogCreate(PydanticBaseModel):
    printer_id: int
    task_id: Optional[int] = None
    task_name: str
    performed_by: Optional[str] = None
    notes: Optional[str] = None
    cost: float = 0
    downtime_minutes: int = 0


# ============== Task templates ==============

@router.get("/maintenance/tasks", tags=["Maintenance"])
def list_maintenance_tasks(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
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


@router.post("/maintenance/tasks", tags=["Maintenance"])
def create_maintenance_task(data: MaintenanceTaskCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def update_maintenance_task(task_id: int, data: MaintenanceTaskUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a maintenance task template."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return {"id": task.id, "message": "Task updated"}


@router.delete("/maintenance/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_task(task_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a maintenance task template and its logs."""
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
):
    """List maintenance logs, optionally filtered by printer."""
    query = db.query(MaintenanceLog).order_by(MaintenanceLog.performed_at.desc())
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
def create_maintenance_log(data: MaintenanceLogCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Log a maintenance action performed on a printer."""
    printer = db.query(Printer).filter(Printer.id == data.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    result = db.execute(text(
        "SELECT COALESCE(SUM(duration_hours), 0) FROM jobs "
        "WHERE printer_id = :pid AND status = 'completed'"
    ), {"pid": data.printer_id}).scalar()
    total_hours = float(result or 0)

    log_entry = MaintenanceLog(
        printer_id=data.printer_id,
        task_id=data.task_id,
        task_name=data.task_name,
        performed_by=data.performed_by,
        notes=data.notes,
        cost=data.cost,
        downtime_minutes=data.downtime_minutes,
        print_hours_at_service=total_hours,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return {"id": log_entry.id, "message": "Maintenance logged", "print_hours_at_service": total_hours}


@router.delete("/maintenance/logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_log(log_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a maintenance log entry."""
    log_entry = db.query(MaintenanceLog).filter(MaintenanceLog.id == log_id).first()
    if not log_entry:
        raise HTTPException(status_code=404, detail="Log not found")
    db.delete(log_entry)
    db.commit()


# ============== Maintenance status ==============

@router.get("/maintenance/status", tags=["Maintenance"])
def get_maintenance_status(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Get maintenance status for all active printers. Returns per-printer task health."""
    printers = db.query(Printer).filter(Printer.is_active.is_(True)).order_by(Printer.name).all()
    tasks = db.query(MaintenanceTask).filter(MaintenanceTask.is_active.is_(True)).all()

    hours_map = {p.id: float(p.total_print_hours or 0) for p in printers}

    all_logs = db.query(MaintenanceLog).all()
    log_map = {}
    for mlog in all_logs:
        key = (mlog.printer_id, mlog.task_id)
        if key not in log_map or (mlog.performed_at and log_map[key].performed_at and mlog.performed_at > log_map[key].performed_at):
            log_map[key] = mlog

    now = datetime.now(timezone.utc)
    result = []

    for printer in printers:
        total_hours = hours_map.get(printer.id, 0)
        printer_tasks = []
        worst_status = "ok"

        for task in tasks:
            if task.printer_model_filter:
                if task.printer_model_filter.lower() not in (printer.model or "").lower():
                    continue

            last_log = log_map.get((printer.id, task.id))

            if last_log and last_log.performed_at:
                hours_since = total_hours - (last_log.print_hours_at_service or 0)
                performed_at = last_log.performed_at
                if performed_at.tzinfo is None:
                    performed_at = performed_at.replace(tzinfo=timezone.utc)
                days_since = (now - performed_at).days
                last_serviced = last_log.performed_at.isoformat()
                last_by = last_log.performed_by
            else:
                hours_since = total_hours
                created = printer.created_at
                if created and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_since = (now - created).days if created else 0
                last_serviced = None
                last_by = None

            task_status = "ok"
            progress = 0.0

            if task.interval_print_hours and task.interval_print_hours > 0:
                pct = (hours_since / task.interval_print_hours) * 100
                progress = max(progress, pct)
                if hours_since >= task.interval_print_hours:
                    task_status = "overdue"
                elif hours_since >= task.interval_print_hours * 0.8:
                    task_status = "due_soon"

            if task.interval_days and task.interval_days > 0:
                pct = (days_since / task.interval_days) * 100
                progress = max(progress, pct)
                if days_since >= task.interval_days:
                    task_status = "overdue"
                elif days_since >= task.interval_days * 0.8:
                    if task_status != "overdue":
                        task_status = "due_soon"

            if task_status == "overdue":
                worst_status = "overdue"
            elif task_status == "due_soon" and worst_status == "ok":
                worst_status = "due_soon"

            printer_tasks.append({
                "task_id": task.id,
                "task_name": task.name,
                "description": task.description,
                "interval_print_hours": task.interval_print_hours,
                "interval_days": task.interval_days,
                "hours_since_service": round(hours_since, 1),
                "days_since_service": days_since,
                "last_serviced": last_serviced,
                "last_by": last_by,
                "status": task_status,
                "progress_percent": round(min(progress, 150), 1),
            })

        result.append({
            "printer_id": printer.id,
            "printer_name": printer.name,
            "printer_model": printer.model,
            "total_print_hours": round(total_hours, 1),
            "tasks": sorted(printer_tasks, key=lambda t: {"overdue": 0, "due_soon": 1, "ok": 2}.get(t["status"], 3)),
            "overall_status": worst_status,
        })

    result.sort(key=lambda p: {"overdue": 0, "due_soon": 1, "ok": 2}.get(p["overall_status"], 3))
    return result


# ============== Seed defaults ==============

@router.post("/maintenance/seed-defaults", tags=["Maintenance"])
def seed_default_maintenance_tasks(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
