"""Queue creation staging and request shapes; caller owns authorization and commit."""
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from pydantic import BaseModel as PydanticBaseModel
from core.base import AlertType, AlertSeverity
from modules.jobs.models import Job

def _stage_prospective_queue_job(db: Session, job: Job, actor: dict):
    """Attest only the inserted billed owner; route authorization stays upstream."""
    from core.economic_identity import _add_created
    from core.print_file_binding import job_file_id, target_printer, require_bound_target
    file_id = job_file_id(db, job.model_id, job.print_file_id)
    bound = target_printer(db, file_id)
    if bound is not None:
        require_bound_target(db, file_id, job.printer_id)
        if job.print_file_id is None:
            from core.print_payload_snapshot import hash_regular_payload, PayloadUnavailable
            from core.errors import OdinError, ErrorCode
            from core.print_payload import validate_plate
            from pathlib import Path
            path = db.execute(text("SELECT stored_path FROM print_files WHERE id=:id"), {"id":file_id}).scalar()
            try:
                job.payload_sha256 = hash_regular_payload(path)
            except (PayloadUnavailable, TypeError):
                raise OdinError(ErrorCode.resource_conflict, "Bound print payload is unavailable", status=409) from None
            job.print_file_id = file_id
            job.plate_index = 0
            if Path(path).suffix.lower() == ".3mf":
                validate_plate(path, 0, "bambu")
        from modules.jobs.pinning import pin_columns
        # Creation-time target columns only; status belongs to the state machine.
        pin = pin_columns(bound)
        job.target_type = pin["target_type"]
        job.target_filter = pin["target_filter"]
    owner = job.charged_to_org_id
    actor_org = actor.get("group_id")
    global_authority = actor.get("role") == "admin" and actor_org is None
    if type(owner) is int and owner > 0 and (
        (type(actor_org) is int and actor_org == owner) or global_authority
    ):
        return _add_created(db, job, "job", owner_org_id=owner)
    if owner is None and global_authority:
        return _add_created(db, job, "job", owner_org_id=None, global_authority=True)
    # Existing unassigned/shared-global creation remains permitted and unknown.
    db.add(job)
    return job


def _stage_prospective_queue_values(db: Session, values: dict, actor: dict):
    """Preserve a raw INSERT's defaults while attesting a fresh queue birth."""
    from sqlalchemy import inspect as inspect_database, null
    allowed = {column.name for column in Job.__table__.columns} - {"id", "economic_uid", "economic_birth_nonce"}
    if type(values) is not dict or set(values) - allowed:
        raise ValueError("Unsupported fresh queue fields")
    fields = dict(values)
    actual_columns = {column["name"]: column for column in inspect_database(db.connection()).get_columns("jobs")}
    for column in Job.__table__.columns:
        if column.name not in fields and column.default is not None:
            actual_default = actual_columns[column.name]["default"]
            fields[column.name] = text(actual_default) if actual_default is not None else null()
    job = Job(**fields)
    _stage_prospective_queue_job(db, job, actor)
    db.flush()
    return job


def _stage_queue_entries(db: Session, jobs: list[Job], user: dict):
    """Stage one creator-scoped notification per inserted queue job."""
    from modules.notifications.alert_dispatcher import stage_alert
    db.flush()
    return [stage_alert(db, AlertType.QUEUE_ADDED, AlertSeverity.INFO,
        f"Job added to queue: {job.item_name or 'Untitled'}",
        job_id=job.id, target_user_ids=[user["id"]]) for job in jobs]


def _deliver_queue_entries(db: Session, plans):
    from modules.notifications.alert_dispatcher import deliver_staged_alert
    for plan in plans:
        deliver_staged_alert(db, plan)


class JobBatchRequest(PydanticBaseModel):
    """Send the same job to multiple printers simultaneously."""
    item_name: str
    model_id: Optional[int] = None
    printer_ids: List[int]
    priority: int = 3
    duration_hours: Optional[float] = None
    colors_required: Optional[str] = None
    filament_type: Optional[str] = None
    notes: Optional[str] = None
    queue_only: bool = False


class JobReorderRequest(PydanticBaseModel):
    job_ids: list[int]


