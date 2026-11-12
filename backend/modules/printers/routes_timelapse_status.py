"""Per-printer timelapse capture status (read-only).

Everything here is derived from rows the capture daemon already writes
(printers, jobs, timelapses, timelapse_publications). No migration, no writer.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from core.db import get_db
from core.printer_utils import is_printing_state
from core.rbac import check_org_access, require_role
from core.timelapse_publication_schema import publications
from modules.printers.camera_urls import get_camera_url
from modules.printers.models import Printer

router = APIRouter()

# A print started outside ODIN is NOT recorded. The capture daemon
# (modules/archives/timelapse_capture.py) selects the running job with
# `jobs.status = 'printing'`, and the capture ticket, ownership anchor and
# audience rules are all keyed to that job. The Bambu monitor creates a jobs row
# for an unscheduled print only when the print ends. Do not flip this constant
# without changing the daemon; tests pin both.
WILL_CAPTURE_EXTERNAL_PRINTS = False

# The daemon captures every 30 s; four missed intervals counts as "not arriving".
STALE_FRAME_SECONDS = 120

# Plain words only: no paths, hosts, credentials or exception text.
MSG_INACTIVE = "This printer is deactivated."
MSG_CAMERA_OFF = "The camera is turned off for this printer."
MSG_NO_CAMERA = "No camera is set up for this printer."
MSG_NOT_PRINTING = "The printer is not printing right now."
MSG_NO_JOB = "This print has no matching ODIN job, so it is not recorded."
MSG_NOT_STARTED = "The recorder has not started for this print."
MSG_BLOCKED = "A frame could not be saved. The recorder will retry."
MSG_STALE = "No new frame has been saved recently."
MESSAGES = (MSG_INACTIVE, MSG_CAMERA_OFF, MSG_NO_CAMERA, MSG_NOT_PRINTING, MSG_NO_JOB, MSG_NOT_STARTED, MSG_BLOCKED, MSG_STALE)

_RECORDING_SQL = text(
    "SELECT t.id, t.print_job_id, t.created_at, t.frame_count, t.ownership_operation_id "
    "FROM timelapses t JOIN jobs j ON j.id = t.print_job_id "
    "WHERE t.printer_id = :pid AND t.status = 'capturing' AND t.retirement_operation_id IS NULL "
    "AND t.ownership_operation_id IS NOT NULL AND j.printer_id = :pid AND j.status = 'printing' "
    "ORDER BY t.id DESC LIMIT 1"
)
_ACTIVE_JOB_SQL = text("SELECT id, actual_start FROM jobs WHERE printer_id = :pid AND status = 'printing' ORDER BY id DESC LIMIT 1")


class TimelapseCaptureStatus(BaseModel):
    enabled: bool
    capturing: bool
    print_job_id: Optional[int] = None
    started_at: Optional[str] = None
    frames_captured: int = 0
    last_frame_at: Optional[str] = None
    last_error: Optional[str] = None
    will_capture_external_prints: bool = WILL_CAPTURE_EXTERNAL_PRINTS


def _as_utc(value) -> Optional[datetime]:
    """Raw SQL on SQLite returns strings; naive values are stored as UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value) -> Optional[str]:
    value = _as_utc(value)
    return value.isoformat() if value else None


def _frame_stats(db: Session, nonce: str):
    """(newest completed frame time, whether the NEWEST frame publication is blocked).

    Only the newest frame counts: an early blocked frame followed by good ones is history."""
    where = (publications.c.kind == "frame", publications.c.source_nonce == nonce)
    newest = db.execute(select(func.max(publications.c.completed_at)).where(*where)).scalar()
    latest_state = db.execute(
        select(publications.c.state).where(*where).order_by(publications.c.frame_number.desc()).limit(1)
    ).scalar()
    return _as_utc(newest), latest_state == "blocked"


def _setup_problem(printer: Printer) -> Optional[str]:
    if not printer.is_active:
        return MSG_INACTIVE
    if not printer.camera_enabled:
        return MSG_CAMERA_OFF
    if not get_camera_url(printer):
        return MSG_NO_CAMERA
    return None


def build_timelapse_status(db: Session, printer: Printer, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    status = {
        "enabled": False,
        "capturing": False,
        "print_job_id": None,
        "started_at": None,
        "frames_captured": 0,
        "last_frame_at": None,
        "last_error": None,
        "will_capture_external_prints": WILL_CAPTURE_EXTERNAL_PRINTS,
    }
    if not printer.timelapse_enabled:
        return status
    status["enabled"] = True
    problem = _setup_problem(printer)
    if problem:
        status["last_error"] = problem
        return status

    printing = is_printing_state(printer.gcode_state)
    recording = db.execute(_RECORDING_SQL, {"pid": printer.id}).mappings().first()
    if recording is None:
        if not printing:
            return status
        job = db.execute(_ACTIVE_JOB_SQL, {"pid": printer.id}).mappings().first()
        if job is None:
            # Printing with no scheduled job at all: the daemon will never record it.
            status["last_error"] = MSG_NO_JOB
        else:
            # A scheduled job but no capture row: fine for the first moments (the daemon has not
            # ticked yet), a stopped recorder after that. An unknown start time is not judged.
            began = _as_utc(job["actual_start"])
            if began is not None and (now - began).total_seconds() > STALE_FRAME_SECONDS:
                status["last_error"] = MSG_NOT_STARTED
        return status
    if not printing:
        status["last_error"] = MSG_NOT_PRINTING
        return status

    started = _as_utc(recording["created_at"])
    last_frame, blocked = _frame_stats(db, recording["ownership_operation_id"])
    status.update(
        capturing=True,
        print_job_id=recording["print_job_id"],
        started_at=_iso(started),
        frames_captured=int(recording["frame_count"] or 0),
        last_frame_at=_iso(last_frame),
    )
    reference = last_frame or started
    if reference is not None and (now - reference).total_seconds() > STALE_FRAME_SECONDS:
        # Nothing is arriving (a stopped recorder, a paused print): not "recording", whatever
        # the capture row says. The counters stay so the page can show what was saved.
        status["capturing"] = False
        status["last_error"] = MSG_STALE
    elif blocked:
        status["last_error"] = MSG_BLOCKED
    return status


@router.get("/printers/{printer_id}/timelapse-status", response_model=TimelapseCaptureStatus, tags=["Cameras"])
def get_timelapse_status(
    printer_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
):
    """Capture status for one printer, with the camera routes' visibility fence."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or (not check_org_access(current_user, printer.org_id) and not printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    from core.controlled_parts import assert_access
    assert_access(db, "printer", printer_id, current_user)
    response.headers["Cache-Control"] = "private, no-store"
    status = build_timelapse_status(db, printer)
    if status["print_job_id"] is not None and not _may_see_job(db, current_user, status["print_job_id"]):
        status["print_job_id"] = None   # the job id follows the job's own visibility, not the printer's
    return status


def _may_see_job(db: Session, user: dict, job_id: int) -> bool:
    """The job routes' rule: the job's organization (not the printer's) and controlled work."""
    from core.controlled_parts import allowed
    row = db.execute(text("SELECT charged_to_org_id FROM jobs WHERE id = :id"), {"id": job_id}).first()
    if row is None:
        return False
    if row[0] is not None and not check_org_access(user, row[0]):
        return False
    return bool(allowed(db, "job", job_id, user))
