"""
PrintFarm Scheduler API

FastAPI application providing REST endpoints for managing
printers, jobs, and the scheduling engine.
"""

from datetime import datetime, timedelta
from typing import List, Optional
from contextlib import asynccontextmanager
import re
import os

from pydantic import BaseModel as PydanticBaseModel, field_validator
from fastapi import FastAPI, Depends, HTTPException, Query, status, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
import httpx

from models import (
    Base, Printer, FilamentSlot, Model, Job, JobStatus, 
    FilamentType, SchedulerRun, init_db
)
from schemas import (
    PrinterCreate, PrinterUpdate, PrinterResponse, PrinterSummary,
    FilamentSlotCreate, FilamentSlotUpdate, FilamentSlotResponse,
    ModelCreate, ModelUpdate, ModelResponse,
    JobCreate, JobUpdate, JobResponse, JobSummary,
    SchedulerConfig as SchedulerConfigSchema, ScheduleResult, SchedulerRunResponse,
    TimelineResponse, TimelineSlot,
    SpoolmanSpool, SpoolmanSyncResult,
    HealthCheck
)
from scheduler import Scheduler, SchedulerConfig, run_scheduler
from config import settings


# Database setup
engine = create_engine(settings.database_url, echo=settings.debug)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Verify API key if authentication is enabled."""
    # If no API key configured, auth is disabled (trusted network mode)
    if not settings.api_key:
        return None
    
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return x_api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    Base.metadata.create_all(bind=engine)
    yield


# Create FastAPI app
app = FastAPI(
    title="PrintFarm Scheduler",
    description="Smart job scheduling for 3D print farms",
    version="0.1.0",
    lifespan=lifespan
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Key authentication middleware
@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """Check API key for all routes except health check."""
    # Skip auth for health endpoint and OPTIONS (CORS preflight)
    if request.url.path == "/health" or request.method == "OPTIONS":
        return await call_next(request)
    
    # If no API key configured, auth is disabled
    if not settings.api_key:
        return await call_next(request)
    
    # Check the API key
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != settings.api_key:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"}
        )
    
    return await call_next(request)


# ============== Health Check ==============

@app.get("/health", response_model=HealthCheck, tags=["System"])
async def health_check():
    """Check API health and connectivity."""
    spoolman_ok = False
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
                spoolman_ok = resp.status_code == 200
        except:
            pass
    
    return HealthCheck(
        status="ok",
        version="0.1.0",
        database=settings.database_url.split("///")[-1],
        spoolman_connected=spoolman_ok
    )


# ============== Printers ==============

@app.get("/api/printers", response_model=List[PrinterResponse], tags=["Printers"])
def list_printers(
    active_only: bool = False,
    db: Session = Depends(get_db)
):
    """List all printers."""
    query = db.query(Printer)
    if active_only:
        query = query.filter(Printer.is_active == True)
    return query.order_by(Printer.display_order, Printer.id).all()


@app.post("/api/printers", response_model=PrinterResponse, status_code=status.HTTP_201_CREATED, tags=["Printers"])
def create_printer(
    printer: PrinterCreate,
    db: Session = Depends(get_db)
):
    """Create a new printer."""
    # Check for duplicate name
    existing = db.query(Printer).filter(Printer.name == printer.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Printer '{printer.name}' already exists")
    
    db_printer = Printer(
        name=printer.name,
        model=printer.model,
        slot_count=printer.slot_count,
        is_active=printer.is_active,
        api_type=printer.api_type,
        api_host=printer.api_host
    )
    db.add(db_printer)
    db.flush()
    
    # Create empty filament slots
    for i in range(1, printer.slot_count + 1):
        slot_data = None
        if printer.initial_slots:
            slot_data = next((s for s in printer.initial_slots if s.slot_number == i), None)
        
        slot = FilamentSlot(
            printer_id=db_printer.id,
            slot_number=i,
            filament_type=slot_data.filament_type if slot_data else FilamentType.PLA,
            color=slot_data.color if slot_data else None,
            color_hex=slot_data.color_hex if slot_data else None
        )
        db.add(slot)
    
    db.commit()
    db.refresh(db_printer)
    return db_printer


@app.get("/api/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def get_printer(printer_id: int, db: Session = Depends(get_db)):
    """Get a specific printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


@app.patch("/api/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def update_printer(
    printer_id: int,
    updates: PrinterUpdate,
    db: Session = Depends(get_db)
):
    """Update a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    update_data = updates.model_dump(exclude_unset=True)
    
    # Handle slot_count changes
    if 'slot_count' in update_data and update_data['slot_count'] != printer.slot_count:
        new_count = update_data['slot_count']
        current_slots = {s.slot_number: s for s in printer.filament_slots}
        current_count = len(current_slots)
        
        if new_count > current_count:
            # Add new slots
            for i in range(current_count + 1, new_count + 1):
                if i not in current_slots:
                    slot = FilamentSlot(
                        printer_id=printer.id,
                        slot_number=i,
                        filament_type=FilamentType.PLA
                    )
                    db.add(slot)
        elif new_count < current_count:
            # Remove extra slots
            for slot in printer.filament_slots:
                if slot.slot_number > new_count:
                    db.delete(slot)
    
    # Apply all updates
    for field, value in update_data.items():
        setattr(printer, field, value)
    
    db.commit()
    db.refresh(printer)
    return printer
@app.delete("/api/printers/{printer_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Printers"])
def delete_printer(printer_id: int, db: Session = Depends(get_db)):
    """Delete a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    db.delete(printer)
    db.commit()


# ============== Filament Slots ==============

@app.get("/api/printers/{printer_id}/slots", response_model=List[FilamentSlotResponse], tags=["Filament"])
def list_filament_slots(printer_id: int, db: Session = Depends(get_db)):
    """List filament slots for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer.filament_slots


@app.patch("/api/printers/{printer_id}/slots/{slot_number}", response_model=FilamentSlotResponse, tags=["Filament"])
def update_filament_slot(
    printer_id: int,
    slot_number: int,
    updates: FilamentSlotUpdate,
    db: Session = Depends(get_db)
):
    """Update a filament slot (e.g., load new filament)."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number
    ).first()
    
    if not slot:
        raise HTTPException(status_code=404, detail="Filament slot not found")
    
    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(slot, field, value)
    
    slot.loaded_at = datetime.utcnow()
    db.commit()
    db.refresh(slot)
    return slot


# ============== Models ==============

@app.get("/api/models", response_model=List[ModelResponse], tags=["Models"])
def list_models(
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all print models."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)
    return query.order_by(Model.name).all()


@app.post("/api/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, tags=["Models"])
def create_model(model: ModelCreate, db: Session = Depends(get_db)):
    """Create a new model definition."""
    # Convert color requirements to dict format
    color_req = None
    if model.color_requirements:
        color_req = {k: v.model_dump() for k, v in model.color_requirements.items()}
    
    db_model = Model(
        name=model.name,
        build_time_hours=model.build_time_hours,
        default_filament_type=model.default_filament_type,
        color_requirements=color_req,
        category=model.category,
        thumbnail_url=model.thumbnail_url,
        notes=model.notes,
        cost_per_item=model.cost_per_item
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


@app.get("/api/models/{model_id}", response_model=ModelResponse, tags=["Models"])
def get_model(model_id: int, db: Session = Depends(get_db)):
    """Get a specific model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@app.patch("/api/models/{model_id}", response_model=ModelResponse, tags=["Models"])
def update_model(model_id: int, updates: ModelUpdate, db: Session = Depends(get_db)):
    """Update a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    update_data = updates.model_dump(exclude_unset=True)
    if "color_requirements" in update_data and update_data["color_requirements"]:
        update_data["color_requirements"] = {
            k: v.model_dump() for k, v in update_data["color_requirements"].items()
        }
    
    for field, value in update_data.items():
        setattr(model, field, value)
    
    db.commit()
    db.refresh(model)
    return model


@app.delete("/api/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Models"])
def delete_model(model_id: int, db: Session = Depends(get_db)):
    """Delete a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    db.delete(model)
    db.commit()


# ============== Jobs ==============

@app.get("/api/jobs", response_model=List[JobResponse], tags=["Jobs"])
def list_jobs(
    status: Optional[JobStatus] = None,
    printer_id: Optional[int] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """List jobs with optional filters."""
    query = db.query(Job)
    
    if status:
        query = query.filter(Job.status == status)
    if printer_id:
        query = query.filter(Job.printer_id == printer_id)
    
    return query.order_by(Job.priority, Job.created_at).offset(offset).limit(limit).all()


@app.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(job: JobCreate, db: Session = Depends(get_db)):
    """Create a new print job."""
    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=job.colors_required,
        filament_type=job.filament_type,
        notes=job.notes,
        hold=job.hold,
        status=JobStatus.PENDING
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)
    return db_job


@app.post("/api/jobs/bulk", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_jobs_bulk(jobs: List[JobCreate], db: Session = Depends(get_db)):
    """Create multiple jobs at once."""
    db_jobs = []
    for job in jobs:
        db_job = Job(
            item_name=job.item_name,
            model_id=job.model_id,
            quantity=job.quantity,
            priority=job.priority,
            duration_hours=job.duration_hours,
            colors_required=job.colors_required,
            filament_type=job.filament_type,
            notes=job.notes,
            hold=job.hold,
            status=JobStatus.PENDING
        )
        db.add(db_job)
        db_jobs.append(db_job)
    
    db.commit()
    for job in db_jobs:
        db.refresh(job)
    return db_jobs


@app.get("/api/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
def get_job(job_id: int, db: Session = Depends(get_db)):
    """Get a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.patch("/api/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
def update_job(job_id: int, updates: JobUpdate, db: Session = Depends(get_db)):
    """Update a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(job, field, value)
    
    db.commit()
    db.refresh(job)
    return job


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Jobs"])
def delete_job(job_id: int, db: Session = Depends(get_db)):
    """Delete a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    db.delete(job)
    db.commit()


@app.post("/api/jobs/{job_id}/start", response_model=JobResponse, tags=["Jobs"])
def start_job(job_id: int, db: Session = Depends(get_db)):
    """Mark a job as started (printing)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status not in [JobStatus.SCHEDULED, JobStatus.PENDING]:
        raise HTTPException(status_code=400, detail=f"Cannot start job in {job.status} status")
    
    job.status = JobStatus.PRINTING
    job.actual_start = datetime.utcnow()
    job.is_locked = True
    
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/jobs/{job_id}/complete", response_model=JobResponse, tags=["Jobs"])
def complete_job(job_id: int, db: Session = Depends(get_db)):
    """Mark a job as completed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job.status = JobStatus.COMPLETED
    job.actual_end = datetime.utcnow()
    job.is_locked = True
    
    # Update printer's loaded colors based on this job
    if job.printer_id and job.colors_list:
        printer = db.query(Printer).filter(Printer.id == job.printer_id).first()
        if printer:
            for i, color in enumerate(job.colors_list[:printer.slot_count]):
                slot = next((s for s in printer.filament_slots if s.slot_number == i + 1), None)
                if slot:
                    slot.color = color
    
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/jobs/{job_id}/fail", response_model=JobResponse, tags=["Jobs"])
def fail_job(job_id: int, notes: Optional[str] = None, db: Session = Depends(get_db)):
    """Mark a job as failed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job.status = JobStatus.FAILED
    job.actual_end = datetime.utcnow()
    job.is_locked = True
    if notes:
        job.notes = f"{job.notes or ''}\nFailed: {notes}".strip()
    
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/jobs/{job_id}/reset", response_model=JobResponse, tags=["Jobs"])
def reset_job(job_id: int, db: Session = Depends(get_db)):
    """Reset a job back to pending status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job.status = JobStatus.PENDING
    job.printer_id = None
    job.scheduled_start = None
    job.scheduled_end = None
    job.actual_start = None
    job.actual_end = None
    job.match_score = None
    job.is_locked = False
    
    db.commit()
    db.refresh(job)
    return job


# ============== Scheduler ==============

@app.post("/api/scheduler/run", response_model=ScheduleResult, tags=["Scheduler"])
def run_scheduler_endpoint(
    config: Optional[SchedulerConfigSchema] = None,
    db: Session = Depends(get_db)
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


@app.get("/api/scheduler/runs", response_model=List[SchedulerRunResponse], tags=["Scheduler"])
def list_scheduler_runs(
    limit: int = Query(default=30, le=100),
    db: Session = Depends(get_db)
):
    """Get scheduler run history."""
    return db.query(SchedulerRun).order_by(SchedulerRun.run_at.desc()).limit(limit).all()


# ============== Timeline ==============

@app.get("/api/timeline", response_model=TimelineResponse, tags=["Timeline"])
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
    printers = db.query(Printer).filter(Printer.is_active == True).all()
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
    
    return TimelineResponse(
        start_date=start_date,
        end_date=end_date,
        slot_duration_minutes=slot_duration,
        printers=printer_summaries,
        slots=slots
    )


# ============== Spoolman Integration ==============

@app.post("/api/spoolman/sync", response_model=SpoolmanSyncResult, tags=["Spoolman"])
async def sync_spoolman(db: Session = Depends(get_db)):
    """Sync filament data from Spoolman."""
    if not settings.spoolman_url:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=10)
            resp.raise_for_status()
            spools = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {e}")
    
    # For now, just return what we found - actual slot matching would need user mapping
    return SpoolmanSyncResult(
        success=True,
        spools_found=len(spools),
        slots_updated=0,
        message=f"Found {len(spools)} spools in Spoolman. Use the UI to assign spools to printer slots."
    )


@app.get("/api/spoolman/spools", response_model=List[SpoolmanSpool], tags=["Spoolman"])
async def list_spoolman_spools():
    """List available spools from Spoolman."""
    if not settings.spoolman_url:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=10)
            resp.raise_for_status()
            spools_data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {e}")
    
    spools = []
    for s in spools_data:
        filament = s.get("filament", {})
        spools.append(SpoolmanSpool(
            id=s.get("id"),
            filament_name=filament.get("name", "Unknown"),
            filament_type=filament.get("material", "PLA"),
            color_name=filament.get("color_name"),
            color_hex=filament.get("color_hex"),
            remaining_weight=s.get("remaining_weight")
        ))
    
    return spools


# ============== Stats ==============

@app.get("/api/stats", tags=["Stats"])
async def get_stats(db: Session = Depends(get_db)):
    """Get dashboard statistics."""
    total_printers = db.query(Printer).count()
    active_printers = db.query(Printer).filter(Printer.is_active == True).count()
    
    pending_jobs = db.query(Job).filter(Job.status == JobStatus.PENDING).count()
    scheduled_jobs = db.query(Job).filter(Job.status == JobStatus.SCHEDULED).count()
    printing_jobs = db.query(Job).filter(Job.status == JobStatus.PRINTING).count()
    completed_today = db.query(Job).filter(
        Job.status == JobStatus.COMPLETED,
        Job.actual_end >= datetime.now().replace(hour=0, minute=0, second=0)
    ).count()
    
    total_models = db.query(Model).count()
    
    # Check Spoolman connection
    spoolman_connected = False
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=3)
                spoolman_connected = resp.status_code == 200
        except:
            pass
    
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
        "spoolman_connected": spoolman_connected
    }

# ============== Spoolman Integration ==============
import httpx

SPOOLMAN_URL = "http://192.168.68.103:7912"

@app.get("/api/spoolman/spools", tags=["Spoolman"])
async def get_spoolman_spools():
    """Fetch all spools from Spoolman."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{SPOOLMAN_URL}/api/v1/spool", timeout=10.0)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {str(e)}")

@app.get("/api/spoolman/filaments", tags=["Spoolman"])
async def get_spoolman_filaments():
    """Fetch all filament types from Spoolman."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{SPOOLMAN_URL}/api/v1/filament", timeout=10.0)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {str(e)}")

# ============== Filament Library ==============
from models import FilamentLibrary

@app.get("/api/filaments", tags=["Filaments"])
def list_filaments(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get filaments from library. If Spoolman is connected, merge with Spoolman data."""
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)
    
    library_filaments = query.all()
    
    # Format response
    result = []
    for f in library_filaments:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })
    
    return result

@app.post("/api/filaments", tags=["Filaments"])
def add_custom_filament(
    brand: str,
    name: str,
    material: str = "PLA",
    color_hex: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Add a custom filament to the library."""
    filament = FilamentLibrary(
        brand=brand,
        name=name,
        material=material,
        color_hex=color_hex,
        is_custom=True
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "message": "Filament added"}

@app.get("/api/filaments/combined", tags=["Filaments"])
async def get_combined_filaments(db: Session = Depends(get_db)):
    """Get filaments from both Spoolman (if available) and local library."""
    result = []
    
    # Try Spoolman first
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=5)
                if resp.status_code == 200:
                    spools = resp.json()
                    for spool in spools:
                        filament = spool.get("filament", {})
                        result.append({
                            "id": f"spool_{spool['id']}",
                            "source": "spoolman",
                            "brand": filament.get("vendor", {}).get("name", "Unknown"),
                            "name": filament.get("name", "Unknown"),
                            "material": filament.get("material", "PLA"),
                            "color_hex": filament.get("color_hex"),
                            "remaining_weight": spool.get("remaining_weight"),
                            "display_name": f"{filament.get('name')} ({filament.get('material')}) - {int(spool.get('remaining_weight', 0))}g",
                        })
        except:
            pass  # Spoolman not available, continue with library
    
    # Add library filaments
    library = db.query(FilamentLibrary).all()
    for f in library:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })
    
    return result

# ============== Filament Library ==============
from models import FilamentLibrary

@app.get("/api/filaments", tags=["Filaments"])
def list_filaments(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get filaments from library. If Spoolman is connected, merge with Spoolman data."""
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)
    
    library_filaments = query.all()
    
    # Format response
    result = []
    for f in library_filaments:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })
    
    return result

@app.post("/api/filaments", tags=["Filaments"])
def add_custom_filament(
    brand: str,
    name: str,
    material: str = "PLA",
    color_hex: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Add a custom filament to the library."""
    filament = FilamentLibrary(
        brand=brand,
        name=name,
        material=material,
        color_hex=color_hex,
        is_custom=True
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "message": "Filament added"}

@app.get("/api/filaments/combined", tags=["Filaments"])
async def get_combined_filaments(db: Session = Depends(get_db)):
    """Get filaments from both Spoolman (if available) and local library."""
    result = []
    
    # Try Spoolman first
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=5)
                if resp.status_code == 200:
                    spools = resp.json()
                    for spool in spools:
                        filament = spool.get("filament", {})
                        result.append({
                            "id": f"spool_{spool['id']}",
                            "source": "spoolman",
                            "brand": filament.get("vendor", {}).get("name", "Unknown"),
                            "name": filament.get("name", "Unknown"),
                            "material": filament.get("material", "PLA"),
                            "color_hex": filament.get("color_hex"),
                            "remaining_weight": spool.get("remaining_weight"),
                            "display_name": f"{filament.get('name')} ({filament.get('material')}) - {int(spool.get('remaining_weight', 0))}g",
                        })
        except:
            pass  # Spoolman not available, continue with library
    
    # Add library filaments
    library = db.query(FilamentLibrary).all()
    for f in library:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })
    
    return result


# ============== Config Schema ==============
class ConfigUpdate(PydanticBaseModel):
    """Validated config update request."""
    spoolman_url: Optional[str] = None
    blackout_start: Optional[str] = None
    blackout_end: Optional[str] = None
    
    @field_validator('spoolman_url')
    @classmethod
    def validate_url(cls, v):
        if v is None or v == '':
            return v
        # Basic URL validation
        url_pattern = re.compile(
            r'^https?://'  # http:// or https://
            r'[a-zA-Z0-9]+'  # domain start
            r'[a-zA-Z0-9.-]*'  # domain rest
            r'(:\d+)?'  # optional port
            r'(/.*)?$'  # optional path
        )
        if not url_pattern.match(v):
            raise ValueError('Invalid URL format. Must be http:// or https://')
        return v
    
    @field_validator('blackout_start', 'blackout_end')
    @classmethod
    def validate_time(cls, v):
        if v is None:
            return v
        # Validate HH:MM format
        time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$')
        if not time_pattern.match(v):
            raise ValueError('Invalid time format. Use HH:MM (e.g., 22:30)')
        return v


# Allowed config keys that can be written to .env
ALLOWED_CONFIG_KEYS = {'SPOOLMAN_URL', 'BLACKOUT_START', 'BLACKOUT_END'}


# ============== Config Endpoints ==============
@app.get("/api/config", tags=["Config"])
def get_config():
    """Get current configuration."""
    return {
        "spoolman_url": settings.spoolman_url,
        "blackout_start": settings.blackout_start,
        "blackout_end": settings.blackout_end,
    }

@app.put("/api/config", tags=["Config"])
def update_config(config: ConfigUpdate):
    """Update configuration. Writes to .env file."""
    # Use environment variable or default path
    env_path = os.environ.get('ENV_FILE_PATH', '/opt/printfarm-scheduler/backend/.env')
    
    # Read existing env, only keeping allowed keys
    env_vars = {}
    try:
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    key, val = line.strip().split('=', 1)
                    # Preserve existing keys (including ones we don't manage like API_KEY)
                    env_vars[key] = val
    except FileNotFoundError:
        pass
    
    # Update only the values that were provided and are allowed
    if config.spoolman_url is not None:
        env_vars['SPOOLMAN_URL'] = config.spoolman_url
    if config.blackout_start is not None:
        env_vars['BLACKOUT_START'] = config.blackout_start
    if config.blackout_end is not None:
        env_vars['BLACKOUT_END'] = config.blackout_end
    
    # Write back
    with open(env_path, 'w') as f:
        for key, val in env_vars.items():
            f.write(f"{key}={val}\n")
    
    return {"success": True, "message": "Config updated. Restart backend to apply changes."}

@app.get("/api/spoolman/test", tags=["Spoolman"])
async def test_spoolman_connection():
    """Test Spoolman connection."""
    if not settings.spoolman_url:
        return {"success": False, "message": "Spoolman URL not configured"}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
            if resp.status_code == 200:
                return {"success": True, "message": f"Connected to Spoolman at {settings.spoolman_url}"}
            else:
                return {"success": False, "message": f"Spoolman returned status {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}

@app.get("/api/analytics", tags=["Analytics"])
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
    
    # Revenue from completed jobs (estimate from model data)
    total_revenue = 0
    total_print_hours = 0
    for job in completed_jobs:
        if job.model_id:
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                total_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity
        if job.duration_hours:
            total_print_hours += job.duration_hours
    
    # Projected revenue from pending jobs
    projected_revenue = 0
    for job in pending_jobs:
        if job.model_id:
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                projected_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity
    
    # Printer utilization
    printers = db.query(Printer).filter(Printer.is_active == True).all()
    printer_stats = []
    for printer in printers:
        printer_jobs = [j for j in completed_jobs if j.printer_id == printer.id]
        hours = sum(j.duration_hours or 0 for j in printer_jobs)
        printer_stats.append({
            "id": printer.id,
            "name": printer.name,
            "completed_jobs": len(printer_jobs),
            "total_hours": round(hours, 1),
        })
    
    # Jobs over time (last 30 days)
    from datetime import datetime, timedelta
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
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
            "projected_revenue": round(projected_revenue, 2),
            "total_print_hours": round(total_print_hours, 1),
            "avg_value_per_hour": round(avg_value_per_hour, 2),
        },
        "printer_stats": printer_stats,
        "jobs_by_date": jobs_by_date,
    }


class MoveJobRequest(PydanticBaseModel):
    printer_id: int
    scheduled_start: datetime

@app.patch("/api/jobs/{job_id}/move", tags=["Jobs"])
def move_job(
    job_id: int,
    request: MoveJobRequest,
    db: Session = Depends(get_db)
):
    """Move a job to a different printer and/or time slot."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Verify printer exists
    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    # Calculate end time based on job duration
    duration_hours = job.effective_duration
    scheduled_end = request.scheduled_start + timedelta(hours=duration_hours)
    
    # Check for conflicts (other jobs on same printer overlapping this time)
    conflict = db.query(Job).filter(
        Job.id != job_id,
        Job.printer_id == request.printer_id,
        Job.status.in_([JobStatus.SCHEDULED, JobStatus.PRINTING]),
        Job.scheduled_start < scheduled_end,
        Job.scheduled_end > request.scheduled_start
    ).first()
    
    if conflict:
        raise HTTPException(
            status_code=400, 
            detail=f"Time slot conflicts with job \'{conflict.item_name}\'"
        )
    
    # Update the job
    job.printer_id = request.printer_id
    job.scheduled_start = request.scheduled_start
    job.scheduled_end = scheduled_end
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.SCHEDULED
    
    db.commit()
    db.refresh(job)
    
    return {
        "success": True,
        "job_id": job.id,
        "printer_id": request.printer_id,
        "scheduled_start": request.scheduled_start.isoformat(),
        "scheduled_end": scheduled_end.isoformat()
    }
