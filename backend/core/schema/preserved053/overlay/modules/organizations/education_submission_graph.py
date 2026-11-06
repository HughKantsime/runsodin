"""Education upload persistence helpers; caller owns transaction boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from modules.organizations.education_membership import retain_membership

from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import inspect, literal_column, null, text
from sqlalchemy.orm import Session

from core.base import FilamentType, JobStatus
from core.db import SessionLocal
from core.db_compat import execute_insert_returning_id
from core.errors import ErrorCode, OdinError
from modules.jobs.models import Job
from modules.organizations.education_storage import MIN_FREE_BYTES, StorageConfigurationError, required_free_bytes





def _lock_job_birth_authority(db: Session, operation_id: str, principal: dict, cost_center_id: int) -> int:
    """Revalidate persisted upload ownership in the graph writer transaction."""
    org_id, user_id = principal.get("group_id"), principal.get("id")
    if any(type(value) is not int or value <= 0 for value in (org_id, user_id, cost_center_id)):
        raise OdinError(ErrorCode.not_found, "Upload authorization changed", status=404)
    connection = db.connection()
    pg = connection.dialect.name == "postgresql"
    from modules.organizations.education_training_policy import lock_center
    # Follow the shared school -> class order before locking upload authority.
    lock_center(db, cost_center_id, org_id)
    if pg:
        # Training locks restore the prior timeout; graph commit retains its cap.
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
    if not pg:
        # A deferred SQLite transaction is not writer authority. This harmless
        # exact-row update acquires the writer lock before any scope reads.
        changed = db.execute(text("UPDATE education_upload_operations SET operation_id=operation_id WHERE operation_id=:id"), {"id": operation_id})
        if changed.rowcount != 1:
            raise OdinError(ErrorCode.not_found, "Upload authorization changed", status=404)
    suffix = " FOR UPDATE NOWAIT" if pg else ""
    user = db.execute(text("SELECT id,group_id,is_active FROM users WHERE id=:id" + suffix), {"id": user_id}).one_or_none()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE NOWAIT'}; values are bound
    center = db.execute(text("SELECT id,org_id,state FROM education_cost_centers WHERE id=:id" + suffix), {"id": cost_center_id}).one_or_none()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE NOWAIT'}; values are bound
    grants = db.execute(text("SELECT user_id,org_id,cost_center_id,role,state FROM education_cost_center_grants WHERE user_id=:user AND org_id=:org AND cost_center_id=:center AND role='student'" + suffix), {"user": user_id, "org": org_id, "center": cost_center_id}).all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE NOWAIT'}; values are bound
    operation = db.execute(text("SELECT operation_id,org_id,user_id,state FROM education_upload_operations WHERE operation_id=:id" + suffix), {"id": operation_id}).one_or_none()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: lock suffix selected from the closed set {'', ' FOR UPDATE NOWAIT'}; values are bound
    if (user is None or tuple(user) != (user_id, org_id, True)
            or center is None or tuple(center) != (cost_center_id, org_id, "active")
            or len(grants) != 1 or tuple(grants[0]) != (user_id, org_id, cost_center_id, "student", "active")
            or operation is None or tuple(operation) != (operation_id, org_id, user_id, "renamed")):
        raise OdinError(ErrorCode.not_found, "Upload authorization changed", status=404)
    return org_id



def _raw_insert_job_defaults(db: Session) -> dict:
    """Preserve omitted raw INSERT defaults instead of introducing ORM defaults."""
    columns = {column["name"]: column for column in inspect(db.connection()).get_columns("jobs")}
    result = {}
    for name in ("quantity_on_bed", "is_favorite", "required_tags", "target_type"):
        default = columns[name]["default"]
        # This is the current database's own default, which the previous raw
        # INSERT executed implicitly. It is never a request/archive expression.
        result[name] = literal_column(default) if default is not None else null()
    return result



def _commit_submission_graph(
    db: Session,
    *,
    operation_id: str,
    principal: dict,
    cost_center_id: int,
    original_filename: str,
    final_path: Path,
    byte_count: int,
    digest: str,
    metadata,
    compatibility_meta: dict,
) -> dict:
    from modules.organizations.education_training_policy import require_submission_training
    org_id = owner._lock_job_birth_authority(db, operation_id, principal, cost_center_id)
    require_submission_training(db, principal=principal, cost_center_id=cost_center_id)
    filaments = [
        {
            "slot": item.slot,
            "type": item.type,
            "color": item.color,
            "used_meters": item.used_meters,
            "used_grams": item.used_grams,
        }
        for item in metadata.filaments
    ]
    print_file_id = execute_insert_returning_id(
        db,
        """INSERT INTO print_files
        (filename,original_filename,project_name,print_time_seconds,total_weight_grams,
         layer_count,layer_height,nozzle_diameter,printer_model,supports_used,bed_type,
         filaments_json,stored_path,bed_x_mm,bed_y_mm,compatible_api_types,file_hash,
         org_id,created_by,storage_bytes,blob_state,compatibility_facts_json)
        VALUES (:filename,:original_filename,:project_name,:print_time,:weight,:layers,
        :layer_height,:nozzle,:printer_model,:supports,:bed_type,:filaments,:stored_path,
        :bed_x,:bed_y,:api_types,:file_hash,:org_id,:created_by,:storage_bytes,'present',:facts)""",
        {
            "filename": original_filename,
            "original_filename": original_filename,
            "project_name": metadata.project_name,
            "print_time": metadata.print_time_seconds,
            "weight": metadata.total_weight_grams,
            "layers": metadata.layer_count,
            "layer_height": metadata.layer_height,
            "nozzle": metadata.nozzle_diameter,
            "printer_model": metadata.printer_model,
            "supports": metadata.supports_used,
            "bed_type": metadata.bed_type,
            "filaments": json.dumps(filaments, sort_keys=True),
            "stored_path": str(final_path),
            "bed_x": compatibility_meta["bed_x_mm"],
            "bed_y": compatibility_meta["bed_y_mm"],
            "api_types": compatibility_meta["compatible_api_types"],
            "file_hash": digest,
            "org_id": org_id,
            "created_by": principal["id"],
            "storage_bytes": byte_count,
            "facts": json.dumps(compatibility_meta["safety_facts"], sort_keys=True),
        },
    )
    primary_material = filaments[0]["type"] if filaments else None
    model_id = execute_insert_returning_id(
        db,
        """INSERT INTO models
        (name,build_time_hours,default_filament_type,print_file_id,category,org_id)
        VALUES (:name,:hours,:material,:file_id,'Education Submission',:org_id)""",
        {
            "name": metadata.project_name,
            "hours": round(float(metadata.print_time_seconds) / 3600.0, 2),
            "material": owner._material_value(primary_material),
            "file_id": print_file_id,
            "org_id": org_id,
        },
    )
    db.execute(
        text("UPDATE print_files SET model_id=:model_id WHERE id=:file_id"),
        {"model_id": model_id, "file_id": print_file_id},
    )
    job = Job(
        model_id=model_id, item_name=metadata.project_name, status=JobStatus.SUBMITTED,
        priority=3, quantity=1, submitted_by=principal["id"],
        charged_to_user_id=principal["id"], charged_to_org_id=org_id,
        hold=False, is_locked=False, print_file_id=print_file_id,
        payload_sha256=digest, plate_index=0, **owner._raw_insert_job_defaults(db),
    )
    owner._add_created(db, job, "job", owner_org_id=org_id)
    job_id = job.id
    submission_id = execute_insert_returning_id(
        db,
        """INSERT INTO education_submissions
        (org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status)
        VALUES (:org_id,:operation_id,:job_id,:file_id,:model_id,:center_id,:user_id,'submitted')""",
        {
            "org_id": org_id,
            "operation_id": operation_id,
            "job_id": job_id,
            "file_id": print_file_id,
            "model_id": model_id,
            "center_id": cost_center_id,
            "user_id": principal["id"],
        },
    )
    operation = owner._operation(db, operation_id)
    reserved = int(operation.reserved_bytes or 0)
    for scope_kind, scope_id in (("user", principal["id"]), ("tenant", org_id)):
        changed = db.execute(
            text(
                "UPDATE education_storage_accounts SET reserved_bytes=reserved_bytes-:reserved, "
                "accounted_bytes=accounted_bytes+:bytes, revision=revision+1, "
                "updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id AND scope_kind=:kind "
                "AND scope_id=:scope_id AND reserved_bytes>=:reserved"
            ),
            {
                "reserved": reserved,
                "bytes": byte_count,
                "org_id": org_id,
                "kind": scope_kind,
                "scope_id": scope_id,
            },
        )
        if changed.rowcount != 1:
            raise RuntimeError("Education storage reservation was lost before graph commit")
    committed_operation = db.execute(
        text(
            "UPDATE education_upload_operations SET state='committed', accounted_bytes=:bytes, "
            "expected_bytes=:bytes, expected_hash=:digest, final_path=:path, staging_path=NULL, "
            "reservation_released_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE operation_id=:id AND state='renamed'"
        ),
        {
            "bytes": byte_count,
            "digest": digest,
            "path": str(final_path),
            "id": operation_id,
        },
    )
    if committed_operation.rowcount != 1:
        raise RuntimeError("Education upload lost its renamed state before graph commit")
    event_id = str(uuid.uuid4())
    request_hash = hashlib.sha256(
        json.dumps(
            {"cost_center_id": cost_center_id, "sha256": digest},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    result = {
        "id": submission_id,
        "job_id": job_id,
        "print_file_id": print_file_id,
        "model_id": model_id,
        "cost_center_id": cost_center_id,
        "status": "submitted",
        "lifecycle_revision": 1,
    }
    db.execute(
        text(
            "INSERT INTO education_audit_events "
            "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,"
            "resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
            "VALUES (:event_id,:org_id,'user',:actor_id,'submission.create',:command_id,:request_hash,"
            "'submission',:resource_id,:center_id,1,:details,:result)"
        ),
        {
            "event_id": event_id,
            "org_id": org_id,
            "actor_id": str(principal["id"]),
            "command_id": operation_id,
            "request_hash": request_hash,
            "resource_id": str(submission_id),
            "center_id": cost_center_id,
            "details": json.dumps({"bytes": byte_count, "sha256": digest}, sort_keys=True),
            "result": json.dumps(result, sort_keys=True),
        },
    )
    recipients = db.execute(
        text(
            "SELECT DISTINCT u.id FROM users u LEFT JOIN education_cost_center_grants g "
            "ON g.user_id=u.id AND g.org_id=u.group_id AND g.cost_center_id=:center_id "
            "AND g.role='manager' AND g.state='active' WHERE u.group_id=:org_id "
            "AND u.is_active IS TRUE AND (u.role='admin' OR g.id IS NOT NULL)"
        ),
        {"center_id": cost_center_id, "org_id": org_id},
    ).fetchall()
    for recipient in recipients:
        db.execute(
            text(
                "INSERT INTO education_notification_outbox (event_id,org_id,recipient_user_id) "
                "VALUES (:event_id,:org_id,:user_id) ON CONFLICT (event_id,recipient_user_id) DO NOTHING"
            ),
            {"event_id": event_id, "org_id": org_id, "user_id": recipient.id},
        )
    return result

# Import after definitions so both the public facade and this module can load first.
import modules.organizations.education_submission_service as owner
