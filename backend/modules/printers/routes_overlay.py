"""Public printer status for streaming overlays; cameras have separate authorization."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.display_names import human_name
from modules.printers.models import Printer
router = APIRouter()

@router.get("/overlay/{printer_id}", tags=["Overlay"])
def get_overlay_data(printer_id: int, db: Session = Depends(get_db)):
    """Public endpoint for OBS streaming overlay. Returns cached printer status."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    # Public endpoint: never expose a stored camera URL (it can embed credentials
    # and may be stored encrypted). Point at the overlay MJPEG relay, which is
    # public in path only: it requires the scoped camera token the copied
    # overlay URL carries (?token=), checked per stream.
    from modules.printers.route_utils import get_camera_url
    camera_url = None
    if printer.is_active and printer.camera_enabled and get_camera_url(printer):
        camera_url = f"/api/overlay/{printer_id}/camera"
    job_row = db.execute(
        text(
            "SELECT job_name, progress_percent, current_layer, total_layers, remaining_minutes "
            "FROM print_jobs WHERE printer_id = :pid AND status = 'running' "
            "ORDER BY started_at DESC LIMIT 1"
        ),
        {"pid": printer_id},
    ).fetchone()
    return {
        "printer_id": printer.id,
        "printer_name": printer.nickname or printer.name,
        "model": printer.model,
        "gcode_state": printer.gcode_state,
        "print_stage": printer.print_stage,
        "print_progress": job_row[1] if job_row else None,
        "current_layer": job_row[2] if job_row else None,
        "total_layers": job_row[3] if job_row else None,
        "time_remaining_min": job_row[4] if job_row else None,
        "nozzle_temp": printer.nozzle_temp,
        "nozzle_target_temp": printer.nozzle_target_temp,
        "bed_temp": printer.bed_temp,
        "bed_target_temp": printer.bed_target_temp,
        "job_name": job_row[0] if job_row else None,
        "display_name": human_name(job_row[0]) if job_row else None,
        "camera_url": camera_url,
    }

