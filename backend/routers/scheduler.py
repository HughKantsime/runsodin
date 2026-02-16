"""O.D.I.N. — Scheduler & Timeline Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timedelta
import logging

from deps import get_db, get_current_user, require_role
from models import (
    Job, JobStatus, Printer, SchedulerRun,
)
from schemas import (
    SchedulerConfig as SchedulerConfigSchema, ScheduleResult, SchedulerRunResponse,
    JobSummary, TimelineResponse, TimelineSlot, PrinterSummary,
)
from scheduler import Scheduler, SchedulerConfig, run_scheduler

log = logging.getLogger("odin.api")

router = APIRouter()


# ──────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────

@router.post("/scheduler/run", response_model=ScheduleResult, tags=["Scheduler"])
def run_scheduler_endpoint(
    config: Optional[SchedulerConfigSchema] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Run the scheduler to assign pending jobs to printers."""
    scheduler_config = None
    if config:
        scheduler_config = SchedulerConfig.from_time_strings(
            blackout_start=config.blackout_start,
            blackout_end=config.blackout_end,
            setup_duration_slots=config.setup_duration_slots,
            horizon_days=config.horizon_days
        )

    result = run_scheduler(db, scheduler_config)

    # Get the run ID from the most recent log
    run_log = db.query(SchedulerRun).order_by(SchedulerRun.id.desc()).first()

    # Get scheduled job summaries
    scheduled_jobs = db.query(Job).filter(
        Job.status == JobStatus.SCHEDULED
    ).order_by(Job.scheduled_start).all()

    job_summaries = []
    for job in scheduled_jobs:
        printer_name = None
        if job.printer:
            printer_name = job.printer.name
        job_summaries.append(JobSummary(
            id=job.id,
            item_name=job.item_name,
            status=job.status,
            priority=job.priority,
            printer_id=job.printer_id,
            printer_name=printer_name,
            scheduled_start=job.scheduled_start,
            scheduled_end=job.scheduled_end,
            duration_hours=job.effective_duration,
            colors_list=job.colors_list,
            match_score=job.match_score
        ))

    return ScheduleResult(
        success=result.success,
        run_id=run_log.id if run_log else 0,
        scheduled=result.scheduled_count,
        skipped=result.skipped_count,
        setup_blocks=result.setup_blocks,
        message=f"Scheduled {result.scheduled_count} jobs, skipped {result.skipped_count}",
        jobs=job_summaries
    )


@router.get("/scheduler/runs", response_model=list[SchedulerRunResponse], tags=["Scheduler"])
def list_scheduler_runs(
    limit: int = Query(default=30, le=100),
    db: Session = Depends(get_db)
):
    """Get scheduler run history."""
    return db.query(SchedulerRun).order_by(SchedulerRun.run_at.desc()).limit(limit).all()


# ──────────────────────────────────────────────
# Timeline
# ──────────────────────────────────────────────

@router.get("/timeline", response_model=TimelineResponse, tags=["Timeline"])
def get_timeline(
    start_date: Optional[datetime] = None,
    days: int = Query(default=7, ge=1, le=30),
    db: Session = Depends(get_db)
):
    """Get timeline view data for the scheduler."""
    if start_date is None:
        start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    end_date = start_date + timedelta(days=days)
    slot_duration = 30  # minutes

    # Get printers
    printers = db.query(Printer).filter(Printer.is_active.is_(True)).all()
    printer_summaries = [
        PrinterSummary(
            id=p.id,
            name=p.name,
            model=p.model,
            is_active=p.is_active,
            loaded_colors=p.loaded_colors
        )
        for p in printers
    ]

    # Get scheduled/printing/completed jobs in range
    jobs = db.query(Job).filter(
        Job.scheduled_start.isnot(None),
        Job.scheduled_start < end_date,
        Job.scheduled_end > start_date,
        Job.status.in_([JobStatus.SCHEDULED, JobStatus.PRINTING, JobStatus.COMPLETED])
    ).all()

    # Build timeline slots
    slots = []
    for job in jobs:
        if job.printer_id is None:
            continue

        printer = next((p for p in printers if p.id == job.printer_id), None)
        if not printer:
            continue

        slots.append(TimelineSlot(
            start=job.scheduled_start,
            end=job.scheduled_end,
            printer_id=job.printer_id,
            printer_name=printer.name,
            job_id=job.id,
            item_name=job.item_name,
            status=job.status,
            is_setup=False,
            colors=job.colors_list
        ))


    # Add MQTT-tracked print jobs to timeline
    mqtt_jobs_query = text("""
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE pj.started_at < :end_date
        AND (pj.ended_at > :start_date OR pj.ended_at IS NULL)
    """)
    mqtt_jobs = db.execute(mqtt_jobs_query, {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat()
    }).fetchall()

    for mj in mqtt_jobs:
        row = dict(mj._mapping)
        printer = next((p for p in printers if p.id == row["printer_id"]), None)
        if not printer:
            continue
        start_time = datetime.fromisoformat(row["started_at"])
        end_time = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else datetime.now()
        mqtt_status = row["status"]
        if mqtt_status == "running":
            job_status = JobStatus.PRINTING
        elif mqtt_status == "completed":
            job_status = JobStatus.COMPLETED
        elif mqtt_status == "failed":
            job_status = JobStatus.FAILED
        else:
            job_status = JobStatus.COMPLETED
        slots.append(TimelineSlot(
            start=start_time,
            end=end_time,
            printer_id=row["printer_id"],
            printer_name=printer.name,
            job_id=None,
            mqtt_job_id=row["id"],
            item_name=row["job_name"] or "MQTT Print",
            status=job_status,
            is_setup=False,
            colors=[]
        ))
    return TimelineResponse(
        start_date=start_date,
        end_date=end_date,
        slot_duration_minutes=slot_duration,
        printers=printer_summaries,
        slots=slots
    )
