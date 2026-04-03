"""Print presets, failure reasons, config, and print-job tracking."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime as dt
import logging

from core.db import get_db
from core.dependencies import get_current_user
from core.rbac import require_role
from modules.jobs.models import Job, PrintPreset
from modules.models_library.models import Model
from modules.jobs.schemas import JobResponse
from core.models import SystemConfig
from license_manager import require_feature

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Jobs"])


FAILURE_REASONS = [
    {"value": "spaghetti", "label": "Spaghetti / Detached"},
    {"value": "adhesion", "label": "Bed Adhesion Failure"},
    {"value": "clog", "label": "Nozzle Clog"},
    {"value": "layer_shift", "label": "Layer Shift"},
    {"value": "stringing", "label": "Excessive Stringing"},
    {"value": "warping", "label": "Warping / Curling"},
    {"value": "filament_runout", "label": "Filament Runout"},
    {"value": "filament_tangle", "label": "Filament Tangle"},
    {"value": "power_loss", "label": "Power Loss"},
    {"value": "firmware_error", "label": "Firmware / HMS Error"},
    {"value": "user_cancelled", "label": "User Cancelled"},
    {"value": "other", "label": "Other"},
]


# ──────────────────────────────────────────────
# Failure reasons
# ──────────────────────────────────────────────

@router.get("/failure-reasons", tags=["Jobs"])
async def get_failure_reasons(current_user: dict = Depends(require_role("viewer"))):
    """List available failure reason categories."""
    return FAILURE_REASONS


# ──────────────────────────────────────────────
# Config: require-job-approval
# ──────────────────────────────────────────────

@router.get("/config/require-job-approval", tags=["Config"])
def get_approval_setting(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get the current job approval requirement setting."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    enabled = False
    if config and config.value in (True, "true", "True", "1"):
        enabled = True
    return {"require_job_approval": enabled}


@router.put("/config/require-job-approval", tags=["Config"])
def set_approval_setting(body: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Toggle the job approval requirement. Admin only. Requires Education tier."""
    require_feature("job_approval")
    enabled = body.get("enabled", False)
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if config:
        config.value = "true" if enabled else "false"
    else:
        config = SystemConfig(key="require_job_approval", value="true" if enabled else "false")
        db.add(config)
    db.commit()
    return {"require_job_approval": enabled}


# ──────────────────────────────────────────────
# Print jobs (MQTT-tracked)
# ──────────────────────────────────────────────

@router.get("/print-jobs", tags=["Print Jobs"])
def get_print_jobs(
    printer_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer")),
):
    """Get print job history from MQTT tracking."""
    # Build query dynamically
    sql = """
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE 1=1
    """
    params = {}

    if printer_id is not None:
        sql += " AND pj.printer_id = :printer_id"
        params["printer_id"] = printer_id
    if status is not None:
        sql += " AND pj.status = :status"
        params["status"] = status

    sql += " ORDER BY pj.started_at DESC LIMIT :limit"
    params["limit"] = limit

    result = db.execute(text(sql), params).fetchall()

    jobs = []
    for row in result:
        job = dict(row._mapping)
        if job.get('ended_at') and job.get('started_at'):
            try:
                start = dt.fromisoformat(job['started_at'])
                end = dt.fromisoformat(job['ended_at'])
                job['duration_minutes'] = round((end - start).total_seconds() / 60, 1)
            except Exception:
                job['duration_minutes'] = None
        else:
            job['duration_minutes'] = None
        jobs.append(job)

    return jobs


@router.get("/print-jobs/stats", tags=["Print Jobs"])
def get_print_job_stats(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Get aggregated print job statistics."""
    query = text("""
        SELECT
            p.id as printer_id,
            p.name as printer_name,
            COUNT(*) as total_jobs,
            SUM(CASE WHEN pj.status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
            SUM(CASE WHEN pj.status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
            SUM(CASE WHEN pj.status = 'running' THEN 1 ELSE 0 END) as running_jobs,
            ROUND(SUM(
                CASE WHEN pj.ended_at IS NOT NULL
                THEN (julianday(pj.ended_at) - julianday(pj.started_at)) * 24
                ELSE 0 END
            ), 2) as total_hours
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        GROUP BY p.id
        ORDER BY total_hours DESC
    """)
    result = db.execute(query).fetchall()
    return [dict(row._mapping) for row in result]


@router.get("/print-jobs/unlinked", tags=["Print Jobs"])
def get_unlinked_print_jobs(printer_id: int = None, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Get recent print jobs not linked to scheduled jobs."""
    sql = """
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE pj.scheduled_job_id IS NULL
    """
    params = {}

    if printer_id:
        sql += " AND pj.printer_id = :printer_id"
        params["printer_id"] = printer_id

    sql += " ORDER BY pj.started_at DESC LIMIT 20"

    result = db.execute(text(sql), params).fetchall()
    return [dict(row._mapping) for row in result]


# ──────────────────────────────────────────────
# Print Presets
# ──────────────────────────────────────────────

@router.get("/presets", tags=["Presets"])
def list_presets(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """List all print presets."""
    presets = db.query(PrintPreset).order_by(PrintPreset.name).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "model_id": p.model_id,
            "model_name": p.model.name if p.model else None,
            "item_name": p.item_name,
            "quantity": p.quantity,
            "priority": p.priority,
            "duration_hours": p.duration_hours,
            "colors_required": p.colors_required,
            "filament_type": p.filament_type.value if p.filament_type else None,
            "required_tags": p.required_tags or [],
            "notes": p.notes,
        }
        for p in presets
    ]


@router.post("/presets", tags=["Presets"], status_code=status.HTTP_201_CREATED)
def create_preset(
    request_data: dict,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Create a new print preset."""
    preset = PrintPreset(
        name=request_data["name"],
        model_id=request_data.get("model_id"),
        item_name=request_data.get("item_name"),
        quantity=request_data.get("quantity", 1),
        priority=request_data.get("priority", 3),
        duration_hours=request_data.get("duration_hours"),
        colors_required=request_data.get("colors_required"),
        filament_type=request_data.get("filament_type"),
        required_tags=request_data.get("required_tags", []),
        notes=request_data.get("notes"),
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return {"id": preset.id, "name": preset.name}


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Presets"])
def delete_preset(
    preset_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Delete a print preset."""
    preset = db.query(PrintPreset).filter(PrintPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")
    db.delete(preset)
    db.commit()


@router.post("/presets/{preset_id}/schedule", tags=["Presets"])
def schedule_from_preset(
    preset_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new job from a preset."""
    preset = db.query(PrintPreset).filter(PrintPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    job = Job(
        item_name=preset.item_name or preset.name,
        model_id=preset.model_id,
        quantity=preset.quantity or 1,
        priority=preset.priority or 3,
        duration_hours=preset.duration_hours,
        colors_required=preset.colors_required,
        filament_type=preset.filament_type,
        required_tags=preset.required_tags or [],
        notes=preset.notes,
    )

    # Cost calculation from model if available
    if preset.model_id:
        model = db.query(Model).filter(Model.id == preset.model_id).first()
        if model:
            if not job.duration_hours and model.build_time_hours:
                job.duration_hours = model.build_time_hours
            if model.estimated_cost:
                job.estimated_cost = model.estimated_cost
            if model.suggested_price:
                job.suggested_price = model.suggested_price

    db.add(job)
    db.commit()
    db.refresh(job)
    return {"id": job.id, "item_name": job.item_name, "status": job.status.value}
