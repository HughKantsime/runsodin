"""Non-route helpers for models_crud.py: revision storage and numbering, 3MF plate summaries,
and the viewer-submission gate and approval alert of schedule_from_model.

Kept apart so models_crud.py stays under the route-file size limit. The revision
transaction and audit helpers stay in models_crud.py (test_audit_atomicity inspects them there).
"""

import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import text

from core.models import SystemConfig
from core.rbac import check_org_access
from modules.models_library.models import Model


def _revision_directory(model_id):
    root = Path(os.environ.get("MODEL_REVISION_STORAGE_ROOT") or "/data/model_revisions")
    if not root.is_absolute():
        raise HTTPException(status_code=400, detail="Revision storage root must be an absolute path")
    root = root.resolve()
    expected = root / str(model_id)
    directory = expected.resolve()
    if directory != expected:
        raise HTTPException(status_code=400, detail="Invalid revision storage path")
    return directory


def _new_revision_file(model_id, number, filename, state):
    directory = _revision_directory(model_id)
    directory.mkdir(parents=True, exist_ok=True)
    # Revalidate after directory creation; reject a model directory alias outside root.
    if directory != _revision_directory(model_id):
        raise HTTPException(status_code=400, detail="Invalid revision storage path")
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', filename or "file")
    destination = directory / f"v{number}_{uuid.uuid4().hex}_{safe_name}"
    stream = open(destination, "xb")
    state["new_file"] = destination
    return destination, stream


def _plate_summaries(path, indices=None):
    """Per-plate sellable pieces / grams / print time from the staged 3MF bytes (model-level
    fields describe plate 1). Restricted to the validated plate indices when given."""
    from modules.models_library.mesh_isolation import Busy, plate_summaries_blocking
    # Parsed in the bounded 3MF child (memory, CPU and deadline caps; same pool as uploads):
    # a hostile plate JSON can never exhaust the API process.
    try:
        analysis = plate_summaries_blocking(path)
    except Busy as busy:
        raise HTTPException(status_code=429, detail="3MF analysis is busy; try again shortly",
                            headers={"Retry-After": str(busy.retry_after)}) from None
    if analysis.status == "plate_json_too_large":
        raise HTTPException(400, "A plate description in this 3MF is too large to read")
    if analysis.status != "ok" or analysis.plates is None:
        raise HTTPException(400, "Original file is not a readable sliced Bambu 3MF")
    return [s for s in analysis.plates if indices is None or s["plate_index"] in indices]


def _locked_revision_model(db, model_id, current_user):
    # A no-op parent write serializes numbering on SQLite and PostgreSQL.
    db.execute(text("UPDATE models SET id=id WHERE id=:id"), {"id": model_id})
    model = db.query(Model).filter(Model.id == model_id).populate_existing().first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")
    return model


def _next_revision_number(db, model_id):
    return (db.execute(text("SELECT MAX(revision_number) FROM model_revisions WHERE model_id=:mid"),
                       {"mid": model_id}).scalar() or 0) + 1


def _require_viewer_submission(db):
    """Viewers submit model jobs only while the job approval workflow is enabled (as POST /jobs)."""
    approval = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if not (approval and approval.value in (True, "true", "True", "1")):
        raise HTTPException(status_code=403, detail="Viewers can only submit jobs for approval when approval is enabled")
    from core.license_manager import require_feature
    require_feature("job_approval")


def _stage_submission_alert(db, current_user, model, job_id):
    """Stage the JOB_SUBMITTED alert for the group owner (else the org's operators/admins)."""
    from core.base import AlertType, AlertSeverity
    from modules.notifications.alert_dispatcher import (
        stage_alert, get_group_owner_id, get_operator_admin_ids,
    )
    owner_id = get_group_owner_id(db, current_user["id"])
    target_ids = [owner_id] if owner_id else get_operator_admin_ids(db, current_user.get("group_id"))
    return stage_alert(db, AlertType.JOB_SUBMITTED, AlertSeverity.INFO,
        f"Job awaiting approval: {model.name or 'Untitled'}",
        f"{current_user.get('display_name') or current_user.get('username', 'A user')} submitted a print job",
        job_id=job_id, target_user_ids=target_ids)
