"""Job CRUD, creation, bulk operations, and queue management."""

from core.rbac import should_filter_org, require_action

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import json
import logging

from modules.jobs.education_guard import assert_generic_job_mutation_allowed, assert_generic_model_reference_allowed
from core.order_production import prepare_job_write, reconcile_job_write
from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import (
    AGENT_READ_SCOPE,
    AGENT_WRITE_SCOPE,
    _get_org_filter,
    check_org_access,
    get_org_scope,
    require_any_scope,
    require_role,
)
from core.responses import build_next_actions, next_action
from core.quota import charge_job_quota
from core.base import JobStatus, AlertType, AlertSeverity
from modules.jobs.models import Job
from modules.printers.models import Printer
from modules.models_library.models import Model
from modules.jobs.schemas import (
    JobCreate, JobUpdate, JobResponse, JobDetailResponse, JobSummary,
)
from core.models import SystemConfig

log = logging.getLogger("odin.api")
logger = log

router = APIRouter(prefix="/jobs", tags=["Jobs"])


from modules.jobs.queue_creation import _stage_prospective_queue_job, _stage_prospective_queue_values, _stage_queue_entries, _deliver_queue_entries, JobBatchRequest, JobReorderRequest

from modules.jobs.pinning import _pin_checked_changes, _target_columns, _target_printer_id  # noqa: E402,F401  (validated pin helpers)


@router.get("/filament-check", tags=["Jobs"])
def check_filament_compatibility(
    printer_id: int,
    filament_type: Optional[str] = None,
    colors: Optional[str] = None,
    current_user: dict = Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Check if a printer has compatible filament loaded. Advisory only — never blocks job creation."""
    from modules.printers.models import FilamentSlot
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")

    warnings = []
    slots = db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).all()

    if filament_type and slots:
        # Empty/unknown slots carry no material; judge only the loaded ones.
        loaded_types = {
            s.filament_type.value.upper() for s in slots
            if s.filament_type and s.filament_type.value.upper() != 'EMPTY'
        }
        if not loaded_types:
            warnings.append(f"Job requires {filament_type} but printer has no filament loaded")
        elif filament_type.upper() not in loaded_types:
            warnings.append(f"Job requires {filament_type} but printer has {', '.join(sorted(loaded_types))} loaded")

    if colors and slots:
        required = [c.strip().lower() for c in colors.split(',') if c.strip()]
        loaded_colors = {(s.color or '').lower() for s in slots if s.color}
        for req_color in required:
            if req_color not in loaded_colors:
                warnings.append(f"Required color '{req_color}' not found in loaded filament slots")

    return {"filament_warnings": warnings, "printer_id": printer_id}


@router.post("/bulk", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_jobs_bulk(jobs: List[JobCreate], current_user: dict = Depends(require_action("jobs.create", scope="write:jobs")), db: Session = Depends(get_db)):
    """Create multiple jobs at once."""
    from modules.models_library.services import calculate_job_cost

    # Pre-load org settings for default filament
    org_settings = {}
    if current_user and current_user.get("group_id"):
        from core.registry import registry
        _org_provider = registry.get_provider("OrgSettingsProvider")
        if _org_provider:
            org_settings = _org_provider.get_org_settings(db, current_user["group_id"])

    # Preflight every Education boundary before quota mutation or job staging.
    pinned = []
    for job in jobs:
        assert_generic_model_reference_allowed(db, job.model_id)
        pinned.append(_target_printer_id(db, current_user, job.printer_id, job.target_type, job.target_filter))
    charge_job_quota(db, current_user, len(jobs))
    db_jobs = []
    for job, pinned_printer_id in zip(jobs, pinned):
        # Calculate cost if model is linked
        estimated_cost, suggested_price, _ = (None, None, None)
        if job.model_id:
            estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)

        effective_filament_type = job.filament_type
        effective_colors = job.colors_required
        if not effective_filament_type and org_settings.get("default_filament_type"):
            effective_filament_type = org_settings["default_filament_type"]
        if not effective_colors and org_settings.get("default_filament_color"):
            effective_colors = org_settings["default_filament_color"]

        db_job = Job(
            item_name=job.item_name,
            model_id=job.model_id,
            quantity=job.quantity,
            priority=job.priority,
            duration_hours=job.duration_hours,
            colors_required=effective_colors,
            filament_type=effective_filament_type,
            notes=job.notes,
            hold=job.hold,
            status=JobStatus.PENDING,
            estimated_cost=estimated_cost,
            suggested_price=suggested_price,
            charged_to_user_id=current_user.get("id") if current_user else None,
            charged_to_org_id=current_user.get("group_id") if current_user else None,
            due_date=job.due_date,
            required_tags=job.required_tags or [],
            **_target_columns(job, pinned_printer_id),
        )
        _stage_prospective_queue_job(db, db_job, current_user)
        db_jobs.append(db_job)

    plans = _stage_queue_entries(db, db_jobs, current_user)
    db.commit()
    _deliver_queue_entries(db, plans)
    for job in db_jobs:
        db.refresh(job)
    return db_jobs


@router.post("/batch", tags=["Jobs"])
def create_jobs_batch(
    body: JobBatchRequest,
    current_user: dict = Depends(require_action("jobs.create", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Send the same job to multiple printers at once (batch production).

    Creates one job per printer_id and returns all created jobs.
    """
    from modules.models_library.services import calculate_job_cost

    if not body.printer_ids:
        raise HTTPException(status_code=400, detail="printer_ids cannot be empty")
    if len(body.printer_ids) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 printers per batch")
    if len(set(body.printer_ids)) != len(body.printer_ids):
        raise HTTPException(status_code=400, detail="printer_ids must be unique")
    if body.model_id is not None:
        model = db.query(Model).filter(Model.id == body.model_id).first()
        if model is None or not check_org_access(current_user, model.org_id):
            raise HTTPException(status_code=404, detail="Model not found")
    assert_generic_model_reference_allowed(db, body.model_id)

    # Validate all printers exist and are accessible
    printers = db.query(Printer).filter(Printer.id.in_(body.printer_ids)).all()
    accessible = [p for p in printers if check_org_access(current_user, p.org_id) or p.shared]
    found_ids = {p.id for p in accessible}
    missing = set(body.printer_ids) - found_ids
    if missing:
        raise HTTPException(status_code=400, detail=f"Printer IDs not found: {sorted(missing)}")

    estimated_cost, suggested_price = None, None
    if body.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=body.model_id)

    charge_job_quota(db, current_user, len(body.printer_ids))
    created = []
    for pid in body.printer_ids:
        db_job = Job(
            item_name=body.item_name,
            model_id=body.model_id,
            quantity=1,
            priority=body.priority,
            printer_id=pid,
            # Sending a job to chosen printers is an explicit choice: pin each copy.
            target_type="specific", target_filter=str(pid),
            duration_hours=body.duration_hours,
            colors_required=body.colors_required,
            filament_type=body.filament_type,
            notes=body.notes,
            hold=body.queue_only,
            status=JobStatus.PENDING,
            estimated_cost=estimated_cost,
            suggested_price=suggested_price,
            charged_to_user_id=current_user.get("id"),
            charged_to_org_id=current_user.get("group_id"),
            submitted_by=current_user.get("id"),
        )
        _stage_prospective_queue_job(db, db_job, current_user)
        created.append(db_job)

    plans = _stage_queue_entries(db, created, current_user)
    db.commit()
    _deliver_queue_entries(db, plans)
    return [{"id": j.id, "printer_id": j.printer_id, "status": j.status.value if hasattr(j.status, 'value') else str(j.status)} for j in created]


# Static route registered before /jobs/{job_id} to prevent FastAPI from
# treating "reorder" as a job_id integer.
@router.patch("/reorder", tags=["Jobs"])
async def reorder_jobs_static(req: JobReorderRequest, current_user: dict = Depends(require_action("jobs.edit", scope="write:jobs")), db: Session = Depends(get_db)):
    """Reorder job queue. Sets queue_position on each job based on array index."""
    reordered = 0
    for position, job_id in enumerate(req.job_ids):
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job or not check_org_access(current_user, job.charged_to_org_id):
            continue
        assert_generic_job_mutation_allowed(db, job_id)
        db.execute(
            text("UPDATE jobs SET queue_position = :pos WHERE id = :id AND status IN ('pending', 'scheduled')"),
            {"pos": position, "id": job_id}
        )
        reordered += 1
    db.commit()
    return {"reordered": reordered}


@router.post("/bulk-update", tags=["Jobs"])
async def bulk_update_jobs(body: dict, current_user: dict = Depends(require_action("jobs.edit", scope="write:jobs")), db: Session = Depends(get_db)):
    """Bulk update job fields (status, priority) for multiple jobs."""
    job_ids = body.get("job_ids", [])
    if not job_ids or not isinstance(job_ids, list):
        raise HTTPException(status_code=400, detail="job_ids list is required")
    if len(job_ids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 jobs per batch")

    if any(isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0 for identifier in job_ids):
        raise HTTPException(400, 'job_ids must contain positive integers')
    job_ids = list(dict.fromkeys(job_ids))

    action = body.get("action", "")
    if action not in {'cancel', 'set_priority', 'delete', 'hold', 'unhold', 'reschedule'}:
        raise HTTPException(400, f'Unknown action: {action}')
    priority = body.get('priority', 3)
    if action == 'set_priority' and (type(priority) is not int or not 1 <= priority <= 5):
        raise HTTPException(400, 'Priority must be an integer from 1 to 5')
    count = 0
    authorized_ids = []
    for identifier in set(job_ids):
        candidate = db.query(Job).filter(Job.id == identifier).first()
        if candidate and check_org_access(current_user, candidate.charged_to_org_id):
            assert_generic_job_mutation_allowed(db, identifier)
            authorized_ids.append(identifier)
    production_orders = prepare_job_write(db, authorized_ids)
    db.expire_all()


    if action == "delete":
        for jid in job_ids:
            job = db.query(Job).filter(Job.id == jid).first()
            if not job or not check_org_access(current_user, job.charged_to_org_id):
                continue
            assert_generic_job_mutation_allowed(db, jid)
            from modules.orders.services import assert_job_history_deletable
            assert_job_history_deletable(db, jid)
            changed = db.execute(text("DELETE FROM jobs WHERE id = :id AND status IN ('pending','submitted','cancelled')"),
                       {"id": jid})
            if changed.rowcount:
                from core.print_attempts import clear_job_attempts, sqlalchemy_runner
                clear_job_attempts(sqlalchemy_runner(db), jid)
                # cert-fix-F's hook also drops the monitor print:<print_job_id> quota markers.
                from core.quota import clear_print_consumption_markers
                clear_print_consumption_markers(sqlalchemy_runner(db), jid)
            count += changed.rowcount
    else:
        from modules.jobs.bulk_actions import apply_bulk_action
        count = apply_bulk_action(db, current_user, action, job_ids, authorized_ids, priority)

    log_audit(db, f"bulk_{action}", "jobs", details=f"{count} jobs")
    reconcile_job_write(db, production_orders)
    db.commit()
    result = {"status": "ok", "affected": count}
    if action == 'reschedule':
        result['skipped'] = len(job_ids) - count
    return result


@router.get("", response_model=List[JobResponse], tags=["Jobs"])
def list_jobs(
    status: Optional[JobStatus] = None,
    printer_id: Optional[int] = None,
    org_id: Optional[int] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    # Stacked auth (Phase 2 canonical read shape).
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE, "read:jobs")
    ),
    db: Session = Depends(get_db),
):
    """List jobs with optional filters."""
    query = db.query(Job).filter(text("NOT EXISTS (SELECT 1 FROM education_submissions es WHERE es.job_id=jobs.id AND es.archived_at IS NOT NULL)"))

    if status:
        query = query.filter(Job.status == status)
    if printer_id:
        query = query.filter(Job.printer_id == printer_id)

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Job.charged_to_org_id == effective_org) | (Job.charged_to_org_id == None))

    # Priority first; drag-and-drop queue_position orders jobs within a
    # priority (unplaced jobs last on SQLite and PostgreSQL alike); then age.
    return query.order_by(
        Job.priority, Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id
    ).offset(offset).limit(limit).all()


@router.post("", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(
    job: JobCreate,
    request: Request,
    db: Session = Depends(get_db),
    # Stacked auth (Phase 2 canonical). Role floor is viewer because
    # viewers can submit jobs for approval (handled internally by the
    # approval_required branch below). agent:write is the scoped-token
    # gate. See routes_controls.py::pause_printer for full rationale.
    current_user: dict = Depends(require_action("jobs.submit")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE, "write:jobs")),
):
    """Create a new print job (queue_job). Agent-surface v1.9.0 Phase 2.

    If approval is required and user is a viewer, job is created as 'submitted'.
    `response_model=JobResponse` intentionally omitted — this route returns a
    dict that includes the job fields plus `next_actions`, which JobResponse
    doesn't model. MCP clients consume the whole body; SPA clients still see
    the job fields they need. If future strictness is wanted, extend
    JobResponse with an optional `next_actions: List[dict]` field.
    """
    # Import here to avoid circular at module level
    from modules.models_library.services import calculate_job_cost

    assert_generic_model_reference_allowed(db, job.model_id)
    pinned_printer_id = _target_printer_id(db, current_user, job.printer_id, job.target_type, job.target_filter)
    # Calculate cost if model is linked
    estimated_cost, suggested_price, _ = (None, None, None)
    if job.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)

    # Check if approval workflow is enabled
    approval_required = False
    approval_config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if approval_config and approval_config.value in (True, "true", "True", "1"):
        approval_required = True

    # Determine initial status
    initial_status = JobStatus.PENDING
    submitted_by = None
    if approval_required and current_user and current_user.get("role") == "viewer":
        from core.license_manager import require_feature
        require_feature("job_approval")
        initial_status = "submitted"
        submitted_by = current_user.get("id")

    # Resolve model_revision_id: use provided value, or default to latest revision
    model_revision_id = getattr(job, 'model_revision_id', None)
    if model_revision_id is None and job.model_id:
        latest_rev = db.execute(text(
            "SELECT id FROM model_revisions WHERE model_id = :mid ORDER BY revision_number DESC LIMIT 1"),
            {"mid": job.model_id}).fetchone()
        if latest_rev:
            model_revision_id = latest_rev.id

    # Apply org default filament if not explicitly set
    effective_filament_type = job.filament_type
    effective_colors = job.colors_required
    if current_user and current_user.get("group_id") and not effective_filament_type:
        from core.registry import registry
        _org_provider = registry.get_provider("OrgSettingsProvider")
        if _org_provider:
            org_settings = _org_provider.get_org_settings(db, current_user["group_id"])
            if org_settings.get("default_filament_type"):
                effective_filament_type = org_settings["default_filament_type"]
            if not effective_colors and org_settings.get("default_filament_color"):
                effective_colors = org_settings["default_filament_color"]

    # Phase 2 dry-run gate. After all preconditions/defaults are resolved but
    # before any DB write or alert dispatch. Agent sees the exact shape the
    # real call would produce.
    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "queue_job",
                "item_name": job.item_name,
                "model_id": job.model_id,
                "model_revision_id": model_revision_id,
                "quantity": job.quantity,
                "priority": job.priority,
                "duration_hours": job.duration_hours,
                "effective_filament_type": effective_filament_type,
                "effective_colors": effective_colors,
                "initial_status": str(initial_status),
                "requires_approval": initial_status == "submitted",
                "estimated_cost": estimated_cost,
                "suggested_price": suggested_price,
                "target_type": job.target_type or "specific",
                "due_date": job.due_date.isoformat() if job.due_date else None,
            },
            next_actions=[
                next_action(
                    "list_queue",
                    {},
                    "see the queue with the new job added",
                ),
            ],
            notes=(
                "Would insert a jobs row; would increment quota usage; "
                "would dispatch JOB_SUBMITTED alert if approval required. "
                "None of these side effects run on dry-run."
            ),
        )

    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        model_revision_id=model_revision_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=effective_colors,
        filament_type=effective_filament_type,
        notes=job.notes,
        hold=job.hold,
        status=initial_status,
        estimated_cost=estimated_cost,
        suggested_price=suggested_price,
        submitted_by=submitted_by,
        due_date=job.due_date,
        charged_to_user_id=current_user["id"] if current_user else None,
        charged_to_org_id=current_user.get("group_id") if current_user else None,
        required_tags=job.required_tags or [],
        **_target_columns(job, pinned_printer_id),
    )
    alert_kind = ("job_submitted" if initial_status == "submitted"
                  else "queue_added" if initial_status == "pending" and current_user else None)

    charge_job_quota(db, current_user)
    _stage_prospective_queue_job(db, db_job, current_user)
    db.flush()
    db.refresh(db_job)
    # Audit is atomic with the job it records; so is the alert it owes (one outbox row,
    # fanned out after the commit by modules.notifications.alert_outbox).
    log_audit(db, "job.created", "job", db_job.id, {"item_name": db_job.item_name, "status": str(initial_status)})
    import modules.notifications.alert_outbox as alert_outbox
    outbox_id = alert_outbox.enqueue(db, db_job.id, alert_kind, current_user["id"]) if alert_kind else None
    # Return JobResponse fields + next_actions as a dict. response_model
    # is intentionally omitted on this route — see docstring. Built from the flushed,
    # refreshed row before the commit, so the response needs no second connection.
    response = JobResponse.model_validate(db_job, from_attributes=True).model_dump(mode="json")
    response["next_actions"] = build_next_actions(
        next_action(
            "get_job",
            {"id": db_job.id},
            "confirm the job landed in the queue",
        ),
        next_action(
            "list_queue",
            {},
            "see queue depth after insertion",
        ),
    )
    # An uncertain commit must not trigger external delivery or automatic retry.
    db.commit()
    if outbox_id is not None:
        # A failure here leaves the row for the replay sweep; it never undoes the job.
        alert_outbox.dispatch(db.get_bind(), outbox_id)
    return response


@router.get("/{job_id}", response_model=JobDetailResponse, tags=["Jobs"])
def get_job(
    job_id: int,
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE, "read:jobs")
    ),
    db: Session = Depends(get_db),
):
    """Get a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    from modules.jobs.ams_mapping import detail_view
    return detail_view(db, job, current_user)


@router.patch("/{job_id}", response_model=JobResponse, tags=["Jobs"])
async def update_job(job_id: int, updates: JobUpdate, request: Request, current_user: dict = Depends(require_action("jobs.edit", scope="write:jobs", fallback="jobs.resubmit")), db: Session = Depends(get_db)):
    """Update a job."""
    # Without jobs.edit, a caller holding jobs.resubmit may only correct their own
    # rejected submission (require_action fallback; viewers by default).
    from core.rbac import RBAC_LIMITED_TO
    correction = current_user.get(RBAC_LIMITED_TO) == 'jobs.resubmit'
    if correction:
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) - {'item_name', 'quantity', 'notes', 'due_date'}:
            raise HTTPException(403, 'Only submission details can be corrected')
        if 'item_name' in raw and (not isinstance(raw['item_name'], str) or not raw['item_name'].strip() or len(raw['item_name']) > 200):
            raise HTTPException(422, 'Choose a nonempty item name of at most 200 characters')
        if 'quantity' in raw and (type(raw['quantity']) is not int or not 1 <= raw['quantity'] <= 10000):
            raise HTTPException(422, 'Choose an explicit quantity from 1 to 10000')
        if 'notes' in raw and raw['notes'] is not None and (not isinstance(raw['notes'], str) or len(raw['notes']) > 10000):
            raise HTTPException(422, 'Notes must be text of at most 10000 characters')

    def allowed_correction(candidate):
        return (candidate is not None and type(current_user.get('id')) is int and current_user['id'] > 0
                and type(current_user.get('group_id')) is int and current_user['group_id'] > 0
                and candidate.charged_to_org_id == current_user['group_id']
                and candidate.submitted_by == current_user['id'] and candidate.status == JobStatus.REJECTED
                and candidate.order_item_id is None
                and db.execute(text('SELECT 1 FROM order_production_jobs WHERE job_id=:id'), {'id':job_id}).first() is None)

    if "status" in updates.model_fields_set:
        # Status only moves through the lifecycle routes (start/complete/fail/
        # cancel/reset and approve/reject/resubmit), which carry the transition
        # guards and the approval gate. A generic PATCH must not bypass them.
        raise HTTPException(
            422,
            "Job status cannot be set by PATCH; use the start, complete, fail, cancel, reset, "
            "approve, reject or resubmit endpoints",
        )
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    if correction and not allowed_correction(job):
        raise HTTPException(403, 'Only your own rejected submission can be corrected')
    assert_generic_job_mutation_allowed(db, job_id)
    production_orders = prepare_job_write(db, [job_id], updates.model_dump(exclude_unset=True))
    if correction:
        try:
            statement = ('SELECT id FROM jobs WHERE id=:id FOR UPDATE NOWAIT'
                         if db.get_bind().dialect.name == 'postgresql' else
                         'UPDATE jobs SET status=status WHERE id=:id')
            db.execute(text(statement), {'id':job_id})
        except Exception:
            db.rollback()
            raise HTTPException(503, 'Job correction temporarily unavailable') from None
    from modules.jobs.ams_mapping import lock_mapping_edit, checked_changes, mapping_audit_details
    lock_mapping_edit(db, job_id, updates)
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    if correction:
        if not allowed_correction(job):
            db.rollback()
            raise HTTPException(409, 'Submission changed; reload before correcting it')
        if db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id'), {'id':job_id}).first():
            db.rollback()
            raise HTTPException(409, 'Submission has an active dispatch claim')
        assert_generic_job_mutation_allowed(db, job_id)

    if (job.print_file_id is not None and "model_id" in updates.model_fields_set
            and updates.model_id != job.model_id):
        raise HTTPException(409, "Archived payload jobs cannot change their source model")
    assert_generic_model_reference_allowed(db, updates.model_id)
    changes = checked_changes(db, current_user, job, _pin_checked_changes(db, current_user, job, updates))
    previous_mapping = job.ams_mapping
    for field, value in changes.items():
        setattr(job, field, value)

    if "ams_mapping" in changes:
        log_audit(db, 'job.ams_mapping_changed', 'job', job.id, mapping_audit_details(job, previous_mapping))
    reconcile_job_write(db, production_orders)
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Jobs"])
def delete_job(job_id: int, current_user: dict = Depends(require_action("jobs.delete", scope="write:jobs")), db: Session = Depends(get_db)):
    """Delete a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)
    from modules.orders.services import assert_job_history_deletable
    production_orders = prepare_job_write(db, [job_id])
    assert_job_history_deletable(db, job_id)
    job = db.query(Job).filter(Job.id == job_id).populate_existing().first()
    if not job or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")

    from core.quota import clear_print_consumption_markers, sqlalchemy_runner as _sar
    clear_print_consumption_markers(_sar(db), job_id)
    log_audit(db, "job.deleted", "job", job.id, {"item_name": job.item_name})
    from core.print_attempts import clear_job_attempts, sqlalchemy_runner
    clear_job_attempts(sqlalchemy_runner(db), job.id)
    db.delete(job)
    reconcile_job_write(db, production_orders)
    db.commit()


@router.post("/{job_id}/repeat", tags=["Jobs"])
async def repeat_job(job_id: int, current_user: dict = Depends(require_action("jobs.create", scope="write:jobs")), db: Session = Depends(get_db)):
    """Clone a job for printing again. Creates a new pending job with same settings."""
    original = db.query(Job).filter(Job.id == job_id).first()
    if not original:
        raise HTTPException(status_code=404, detail="Job not found")
    if not check_org_access(current_user, original.charged_to_org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    assert_generic_job_mutation_allowed(db, job_id)

    assert_generic_model_reference_allowed(db, original.model_id)
    if original.print_file_id is not None:
        # Repeating must retain the archived payload, not reselect a model file.
        from modules.printers.models import Printer
        target = db.query(Printer).filter(Printer.id == original.printer_id).first()
        if not target or not (check_org_access(current_user, target.org_id) or target.shared):
            raise HTTPException(404, "Printer not found")
        payload = db.execute(text("SELECT pf.model_id,pf.stored_path,pf.org_id,m.org_id AS model_org "
                                  "FROM print_files pf JOIN models m ON m.id=pf.model_id WHERE pf.id=:id"),
                             {"id": original.print_file_id}).first()
        if db.execute(text("SELECT 1 FROM education_submissions WHERE print_file_id=:id"),
                      {"id": original.print_file_id}).first():
            raise HTTPException(409, "Education files require their approval workflow")
        if payload and (not check_org_access(current_user, payload.org_id)
                        or not check_org_access(current_user, payload.model_org)):
            raise HTTPException(404, "Original print file not found")
        if not payload or payload.model_id != original.model_id:
            raise HTTPException(400, "Original print file not available for repeat")
    # Create new job with same settings
    new_job = Job(
        model_id=original.model_id,
        print_file_id=original.print_file_id,
        payload_sha256=original.payload_sha256,
        plate_index=original.plate_index,
        ams_mapping=original.ams_mapping,
        charged_to_org_id=original.charged_to_org_id,
        charged_to_user_id=current_user.get("id"),
        submitted_by=current_user.get("id"),
        item_name=original.item_name,
        quantity=original.quantity,
        status="pending",
        priority=original.priority,
        printer_id=original.printer_id,  # Same printer preference (not a pin)
        # Same target (an explicit pin, model or protocol target) as the original.
        target_type=original.target_type,
        target_filter=original.target_filter,
        duration_hours=original.duration_hours,
        colors_required=original.colors_required,
        filament_type=original.filament_type,
        notes=f"Repeat of job #{job_id}" + (f" - {original.notes}" if original.notes else ""),
        estimated_cost=original.estimated_cost,
        suggested_price=original.suggested_price,
        quantity_on_bed=original.quantity_on_bed,
    )

    charge_job_quota(db, current_user)
    _stage_prospective_queue_job(db, new_job, current_user)
    plans = _stage_queue_entries(db, [new_job], current_user)
    db.commit()
    _deliver_queue_entries(db, plans)
    db.refresh(new_job)

    return {
        "success": True,
        "message": f"Job cloned successfully",
        "original_job_id": job_id,
        "new_job_id": new_job.id
    }
