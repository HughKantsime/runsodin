"""Durable streamed upload state machine for Education submissions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.base import FilamentType
from core.db import SessionLocal
from core.db_compat import execute_insert_returning_id
from core.errors import ErrorCode, OdinError
from modules.models_library.threemf_parser import parse_3mf


CHUNK_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
USER_STORAGE_CAP_BYTES = 2 * 1024 * 1024 * 1024
TENANT_STORAGE_CAP_BYTES = 50 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024
UPLOAD_ROOT = Path(os.environ.get("EDUCATION_UPLOAD_ROOT", "/data/education_uploads"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bucket_starts(now: datetime) -> tuple[datetime, datetime]:
    normalized = now.astimezone(timezone.utc)
    return (
        normalized.replace(minute=0, second=0, microsecond=0),
        normalized.replace(hour=0, minute=0, second=0, microsecond=0),
    )


def _operation(db: Session, operation_id: str):
    return db.execute(
        text("SELECT * FROM education_upload_operations WHERE operation_id=:id"),
        {"id": operation_id},
    ).fetchone()


def _submission_result(db: Session, operation_id: str):
    return db.execute(
        text(
            "SELECT id,job_id,print_file_id,model_id,cost_center_id,status,lifecycle_revision "
            "FROM education_submissions WHERE operation_id=:id"
        ),
        {"id": operation_id},
    ).fetchone()


def _resolve_existing_attempt(
    db: Session, *, existing, principal: dict, cost_center_id: int
) -> dict | None:
    org_id = int(principal["group_id"])
    if int(existing.org_id) != org_id or int(existing.user_id) != int(principal["id"]):
        raise OdinError(ErrorCode.not_found, "Submission not found", status=404)
    if existing.state == "needs_reconciliation":
        reconcile_upload_operation(db, operation_id=str(existing.operation_id))
        existing = _operation(db, str(existing.operation_id))
    if existing.state == "committed":
        row = _submission_result(db, str(existing.operation_id))
        if not row:
            raise OdinError(
                ErrorCode.internal_error,
                "Committed upload is missing its submission graph",
                status=503,
                retriable=True,
            )
        if int(row.cost_center_id) != int(cost_center_id):
            raise OdinError(
                ErrorCode.idempotency_conflict,
                "submission_token was reused with a different cost center",
                status=409,
            )
        return dict(row._mapping)
    raise OdinError(
        ErrorCode.idempotency_conflict,
        "submission_token is already in progress or awaiting reconciliation",
        status=409,
        retriable=existing.state == "needs_reconciliation",
    )


def validate_replay_identity(
    db: Session, *, operation_id: str, byte_count: int, digest: str
) -> None:
    operation = _operation(db, operation_id)
    if (
        not operation
        or operation.state != "committed"
        or int(operation.expected_bytes or -1) != int(byte_count)
        or str(operation.expected_hash or "") != digest
    ):
        raise OdinError(
            ErrorCode.idempotency_conflict,
            "submission_token was reused with a different file",
            status=409,
        )


def _admit_counter(
    db: Session,
    *,
    org_id: int,
    scope_kind: str,
    scope_id: int,
    user_id: int | None,
    bucket_kind: str,
    bucket_start: datetime,
    limit: int,
) -> bool:
    db.execute(
        text(
            "INSERT INTO education_rate_counters "
            "(org_id, scope_kind, scope_id, user_id, bucket_kind, bucket_start, attempt_count) "
            "VALUES (:org_id,:scope_kind,:scope_id,:user_id,:bucket_kind,:bucket_start,0) "
            "ON CONFLICT (org_id,scope_kind,scope_id,bucket_kind,bucket_start) DO NOTHING"
        ),
        {
            "org_id": org_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "user_id": user_id,
            "bucket_kind": bucket_kind,
            "bucket_start": bucket_start,
        },
    )
    changed = db.execute(
        text(
            "UPDATE education_rate_counters SET attempt_count=attempt_count+1, "
            "updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id AND scope_kind=:scope_kind "
            "AND scope_id=:scope_id AND bucket_kind=:bucket_kind "
            "AND bucket_start=:bucket_start AND attempt_count < :limit"
        ),
        {
            "org_id": org_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "bucket_kind": bucket_kind,
            "bucket_start": bucket_start,
            "limit": limit,
        },
    )
    return changed.rowcount == 1


def begin_upload_attempt(
    db: Session,
    *,
    operation_id: str,
    principal: dict,
    cost_center_id: int,
    now: datetime | None = None,
) -> dict | None:
    """Persist one admitted attempt; return a committed replay snapshot if present."""
    try:
        uuid.UUID(operation_id)
    except ValueError as exc:
        raise OdinError(
            ErrorCode.validation_failed,
            "submission_token must be a UUID",
            status=422,
            extra={"fields": ["submission_token"]},
        ) from exc
    org_id = principal.get("group_id")
    if org_id is None:
        raise OdinError(ErrorCode.permission_denied, "Tenant membership is required", status=403)
    existing = _operation(db, operation_id)
    if existing:
        return _resolve_existing_attempt(
            db,
            existing=existing,
            principal=principal,
            cost_center_id=cost_center_id,
        )

    grant = db.execute(
        text(
            "SELECT c.id FROM education_cost_centers c "
            "JOIN education_cost_center_grants g ON g.cost_center_id=c.id AND g.org_id=c.org_id "
            "WHERE c.id=:center_id AND c.org_id=:org_id AND c.state='active' "
            "AND g.user_id=:user_id AND g.role='student' AND g.state='active'"
        ),
        {"center_id": cost_center_id, "org_id": org_id, "user_id": principal["id"]},
    ).fetchone()
    if not grant:
        raise OdinError(ErrorCode.not_found, "Cost center not found", status=404)

    claimed = db.execute(
        text(
            "INSERT INTO education_upload_operations "
            "(operation_id,org_id,user_id,state) VALUES (:id,:org_id,:user_id,'reserved') "
            "ON CONFLICT (operation_id) DO NOTHING"
        ),
        {"id": operation_id, "org_id": org_id, "user_id": principal["id"]},
    )
    if claimed.rowcount != 1:
        db.rollback()
        existing = _operation(db, operation_id)
        if not existing:
            raise OdinError(
                ErrorCode.internal_error,
                "Unable to resolve submission token claim",
                status=503,
                retriable=True,
            )
        return _resolve_existing_attempt(
            db,
            existing=existing,
            principal=principal,
            cost_center_id=cost_center_id,
        )

    hour_start, day_start = _bucket_starts(now or _utc_now())
    admitted = (
        _admit_counter(
            db,
            org_id=int(org_id),
            scope_kind="user",
            scope_id=int(principal["id"]),
            user_id=int(principal["id"]),
            bucket_kind="hour",
            bucket_start=hour_start,
            limit=10,
        )
        and _admit_counter(
            db,
            org_id=int(org_id),
            scope_kind="user",
            scope_id=int(principal["id"]),
            user_id=int(principal["id"]),
            bucket_kind="day",
            bucket_start=day_start,
            limit=30,
        )
        and _admit_counter(
            db,
            org_id=int(org_id),
            scope_kind="tenant",
            scope_id=int(org_id),
            user_id=None,
            bucket_kind="hour",
            bucket_start=hour_start,
            limit=100,
        )
    )
    if not admitted:
        db.rollback()
        raise OdinError(
            ErrorCode.rate_limited,
            "Education upload attempt limit reached",
            status=429,
            retriable=True,
        )
    db.commit()
    return None


def _ensure_storage_account(
    db: Session, *, org_id: int, scope_kind: str, scope_id: int, user_id: int | None
) -> None:
    db.execute(
        text(
            "INSERT INTO education_storage_accounts "
            "(org_id,scope_kind,scope_id,user_id,reserved_bytes,accounted_bytes) "
            "VALUES (:org_id,:scope_kind,:scope_id,:user_id,0,0) "
            "ON CONFLICT (org_id,scope_kind,scope_id) DO NOTHING"
        ),
        {
            "org_id": org_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "user_id": user_id,
        },
    )


def reserve_storage_chunk(
    db: Session, *, operation_id: str, amount: int, storage_root: Path = UPLOAD_ROOT
) -> None:
    if amount <= 0 or amount > CHUNK_BYTES:
        raise ValueError("reservation amount must be one positive upload chunk")
    usage = shutil.disk_usage(storage_root)
    required_free = max(MIN_FREE_BYTES, int(usage.total * 0.10))
    if usage.free - amount < required_free:
        raise OdinError(
            ErrorCode.quota_exceeded,
            "Upload would violate the Education storage headroom reserve",
            status=507,
            retriable=True,
        )
    operation = _operation(db, operation_id)
    if not operation or operation.state not in ("reserved", "streaming"):
        raise OdinError(ErrorCode.invalid_state_transition, "Upload is not streamable", status=409)
    org_id = int(operation.org_id)
    user_id = int(operation.user_id)
    _ensure_storage_account(
        db, org_id=org_id, scope_kind="user", scope_id=user_id, user_id=user_id
    )
    _ensure_storage_account(
        db, org_id=org_id, scope_kind="tenant", scope_id=org_id, user_id=None
    )
    for scope_kind, scope_id, cap in (
        ("user", user_id, USER_STORAGE_CAP_BYTES),
        ("tenant", org_id, TENANT_STORAGE_CAP_BYTES),
    ):
        changed = db.execute(
            text(
                "UPDATE education_storage_accounts SET reserved_bytes=reserved_bytes+:amount, "
                "revision=revision+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE org_id=:org_id AND scope_kind=:scope_kind AND scope_id=:scope_id "
                "AND reserved_bytes+accounted_bytes+:amount <= :cap"
            ),
            {
                "amount": amount,
                "org_id": org_id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "cap": cap,
            },
        )
        if changed.rowcount != 1:
            db.rollback()
            raise OdinError(
                ErrorCode.quota_exceeded,
                f"Education {scope_kind} storage quota exceeded",
                status=413,
            )
    db.execute(
        text(
            "UPDATE education_upload_operations SET state='streaming', "
            "reserved_bytes=reserved_bytes+:amount, updated_at=CURRENT_TIMESTAMP "
            "WHERE operation_id=:id"
        ),
        {"amount": amount, "id": operation_id},
    )
    db.commit()


def _release_reservation(db: Session, operation) -> bool:
    if operation.reservation_released_at is not None:
        return False
    amount = int(operation.reserved_bytes or 0)
    claimed = db.execute(
        text(
            "UPDATE education_upload_operations SET reservation_released_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id "
            "AND reservation_released_at IS NULL"
        ),
        {"id": operation.operation_id},
    )
    if claimed.rowcount != 1:
        return False
    if amount == 0:
        return True
    for scope_kind, scope_id in (
        ("user", int(operation.user_id)),
        ("tenant", int(operation.org_id)),
    ):
        changed = db.execute(
            text(
                "UPDATE education_storage_accounts SET "
                "reserved_bytes=reserved_bytes-:amount, "
                "revision=revision+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE org_id=:org_id AND scope_kind=:scope_kind AND scope_id=:scope_id "
                "AND reserved_bytes>=:amount"
            ),
            {
                "amount": amount,
                "org_id": operation.org_id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
            },
        )
        if changed.rowcount != 1:
            raise RuntimeError("Education reservation accounting is inconsistent")
    return True


def abort_upload(db: Session, *, operation_id: str, paths: tuple[Path, ...]) -> None:
    cleanup_error = None
    for path in paths:
        try:
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed and path.parent.exists():
                _fsync_directory(path.parent)
        except OSError as exc:
            cleanup_error = exc
    operation = _operation(db, operation_id)
    if not operation:
        return
    if cleanup_error:
        db.execute(
            text(
                "UPDATE education_upload_operations SET state='needs_reconciliation', "
                "cleanup_path=COALESCE(cleanup_path,final_path,staging_path), "
                "updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id"
            ),
            {"id": operation_id},
        )
        db.commit()
        return
    _release_reservation(db, operation)
    db.execute(
        text(
            "UPDATE education_upload_operations SET state='aborted', cleanup_path=NULL, "
            "updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id"
        ),
        {"id": operation_id},
    )
    db.commit()


def reconcile_upload_operation(db: Session, *, operation_id: str) -> dict | None:
    """Resolve an uncertain commit from a fresh graph check or durable cleanup."""
    operation = _operation(db, operation_id)
    if not operation or operation.state != "needs_reconciliation":
        row = _submission_result(db, operation_id)
        return dict(row._mapping) if row else None
    row = _submission_result(db, operation_id)
    if row:
        changed = db.execute(
            text(
                "UPDATE education_upload_operations SET state='committed',cleanup_path=NULL,"
                "updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id "
                "AND state='needs_reconciliation' AND reservation_released_at IS NOT NULL"
            ),
            {"id": operation_id},
        )
        if changed.rowcount != 1:
            db.rollback()
            raise OdinError(
                ErrorCode.internal_error,
                "Committed submission has inconsistent storage accounting",
                status=503,
                retriable=True,
            )
        db.commit()
        return dict(row._mapping)
    paths = tuple(
        Path(value)
        for value in (operation.staging_path, operation.final_path, operation.cleanup_path)
        if value
    )
    abort_upload(db, operation_id=operation_id, paths=paths)
    return None


def _material_value(raw: str | None) -> str:
    return FilamentType.from_bambu_code(raw or "").value


def _compatibility_meta_from_parsed(metadata) -> dict:
    """Project parsed safety evidence into legacy persistence columns."""
    facts = metadata.safety_facts
    bed_fact = facts.get("bed") or {}
    bed = bed_fact.get("value") if bed_fact.get("present") else None
    bed = bed if isinstance(bed, dict) else {}
    api_fact = facts.get("api_types") or {}
    api_values = (
        api_fact.get("value")
        if api_fact.get("present") and api_fact.get("recognized")
        else []
    )
    return {
        "bed_x_mm": bed.get("x_mm"),
        "bed_y_mm": bed.get("y_mm"),
        "compatible_api_types": ",".join(
            sorted(
                {
                    str(value).strip().lower()
                    for value in (api_values or [])
                    if str(value).strip()
                }
            )
        ),
        "safety_facts": facts,
    }


def hash_upload_source(source: BinaryIO) -> tuple[int, str]:
    total = 0
    digest = hashlib.sha256()
    while True:
        chunk = source.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise OdinError(
                ErrorCode.quota_exceeded, "File exceeds 100 MiB upload limit", status=413
            )
        digest.update(chunk)
    return total, digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fresh_session_factory(db: Session):
    if db.get_bind() is SessionLocal.kw.get("bind"):
        return SessionLocal
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=db.get_bind())


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
    org_id = int(principal["group_id"])
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
            "material": _material_value(primary_material),
            "file_id": print_file_id,
            "org_id": org_id,
        },
    )
    db.execute(
        text("UPDATE print_files SET model_id=:model_id WHERE id=:file_id"),
        {"model_id": model_id, "file_id": print_file_id},
    )
    job_id = execute_insert_returning_id(
        db,
        """INSERT INTO jobs
        (model_id,item_name,status,priority,quantity,submitted_by,charged_to_user_id,charged_to_org_id)
        VALUES (:model_id,:name,'submitted',3,1,:user_id,:user_id,:org_id)""",
        {
            "model_id": model_id,
            "name": metadata.project_name,
            "user_id": principal["id"],
            "org_id": org_id,
        },
    )
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
    operation = _operation(db, operation_id)
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


def prepare_upload_paths(
    db: Session,
    *,
    operation_id: str,
    principal: dict,
    original_filename: str,
    storage_root: Path = UPLOAD_ROOT,
) -> tuple[Path, Path]:
    if Path(original_filename).suffix.lower() != ".3mf":
        raise OdinError(
            ErrorCode.validation_failed,
            "Education submissions require sliced .3mf",
            status=400,
        )
    staging_dir = storage_root / "staging"
    final_dir = storage_root / str(principal["group_id"])
    staging_path = staging_dir / f"{operation_id}.part"
    final_path = final_dir / f"{operation_id}.3mf"
    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    final_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    changed = db.execute(
        text(
            "UPDATE education_upload_operations SET staging_path=:staging,final_path=:final,"
            "updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id AND state='reserved'"
        ),
        {"staging": str(staging_path), "final": str(final_path), "id": operation_id},
    )
    if changed.rowcount != 1:
        db.rollback()
        raise OdinError(ErrorCode.invalid_state_transition, "Upload is not reservable", status=409)
    db.commit()
    return staging_path, final_path


def finalize_staged_3mf_submission(
    db: Session,
    *,
    original_filename: str,
    operation_id: str,
    principal: dict,
    cost_center_id: int,
    staging_path: Path,
    final_path: Path,
    byte_count: int,
    digest: str,
) -> dict:
    """Validate a fully reserved staging file and atomically commit its graph."""
    renamed = False
    try:
        if byte_count == 0:
            raise OdinError(ErrorCode.validation_failed, "Upload is empty", status=400)
        staged = db.execute(
            text(
                "UPDATE education_upload_operations SET state='staged',expected_bytes=:bytes,"
                "expected_hash=:digest,updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id "
                "AND state='streaming' AND reserved_bytes=:bytes"
            ),
            {"bytes": byte_count, "digest": digest, "id": operation_id},
        )
        if staged.rowcount != 1:
            raise RuntimeError("Education upload reservation did not match staged bytes")
        db.commit()
        try:
            with zipfile.ZipFile(staging_path) as archive:
                if sum(item.file_size for item in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                    raise OdinError(ErrorCode.validation_failed, "Expanded .3mf exceeds 500 MiB", status=400)
        except zipfile.BadZipFile as exc:
            raise OdinError(ErrorCode.validation_failed, "Invalid .3mf container", status=400) from exc
        metadata = parse_3mf(str(staging_path))
        if not metadata or metadata.print_time_seconds <= 0:
            raise OdinError(ErrorCode.validation_failed, "Education upload must be a sliced .3mf", status=400)
        compatibility_meta = _compatibility_meta_from_parsed(metadata)
        os.replace(staging_path, final_path)
        _fsync_directory(final_path.parent)
        renamed = True
        try:
            changed = db.execute(
                text(
                    "UPDATE education_upload_operations SET state='renamed',staging_path=NULL,"
                    "final_path=:path,updated_at=CURRENT_TIMESTAMP "
                    "WHERE operation_id=:id AND state='staged'"
                ),
                {"path": str(final_path), "id": operation_id},
            )
            if changed.rowcount != 1:
                raise RuntimeError("Education upload lost its staged state before rename commit")
            db.commit()
        except Exception:
            db.rollback()
            with _fresh_session_factory(db)() as fresh:
                fresh.execute(
                    text(
                        "UPDATE education_upload_operations SET state='needs_reconciliation',"
                        "cleanup_path=final_path,updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                fresh.commit()
            raise
        try:
            result = _commit_submission_graph(
                db,
                operation_id=operation_id,
                principal=principal,
                cost_center_id=cost_center_id,
                original_filename=Path(original_filename).name,
                final_path=final_path,
                byte_count=byte_count,
                digest=digest,
                metadata=metadata,
                compatibility_meta=compatibility_meta,
            )
        except Exception:
            db.rollback()
            abort_upload(db, operation_id=operation_id, paths=(staging_path, final_path))
            raise
        try:
            db.commit()
            return result
        except Exception:
            db.rollback()
            with _fresh_session_factory(db)() as fresh:
                committed = fresh.execute(
                    text(
                        "SELECT s.id,s.job_id,s.print_file_id,s.model_id,s.cost_center_id,s.status,"
                        "s.lifecycle_revision FROM education_submissions s WHERE s.operation_id=:id"
                    ),
                    {"id": operation_id},
                ).fetchone()
                if committed:
                    return dict(committed._mapping)
                fresh.execute(
                    text(
                        "UPDATE education_upload_operations SET state='needs_reconciliation',"
                        "cleanup_path=final_path,updated_at=CURRENT_TIMESTAMP WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                fresh.commit()
            raise
    except Exception:
        if not renamed:
            abort_upload(db, operation_id=operation_id, paths=(staging_path, final_path))
        raise


def process_sliced_3mf_submission(
    db: Session,
    *,
    source: BinaryIO,
    original_filename: str,
    operation_id: str,
    principal: dict,
    cost_center_id: int,
    storage_root: Path = UPLOAD_ROOT,
) -> dict:
    """Stream, validate, retain, and atomically create an Education submission."""
    replay = begin_upload_attempt(
        db,
        operation_id=operation_id,
        principal=principal,
        cost_center_id=cost_center_id,
    )
    if replay:
        byte_count, replay_digest = hash_upload_source(source)
        validate_replay_identity(
            db,
            operation_id=operation_id,
            byte_count=byte_count,
            digest=replay_digest,
        )
        return replay
    staging_path = final_path = None
    total = 0
    digest = hashlib.sha256()
    try:
        staging_path, final_path = prepare_upload_paths(
            db,
            operation_id=operation_id,
            principal=principal,
            original_filename=original_filename,
            storage_root=storage_root,
        )
        with open(staging_path, "xb") as destination:
            while True:
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                if total + len(chunk) > MAX_UPLOAD_BYTES:
                    raise OdinError(
                        ErrorCode.quota_exceeded,
                        "File exceeds 100 MiB upload limit",
                        status=413,
                    )
                reserve_storage_chunk(
                    db,
                    operation_id=operation_id,
                    amount=len(chunk),
                    storage_root=storage_root,
                )
                destination.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        abort_upload(
            db,
            operation_id=operation_id,
            paths=tuple(path for path in (staging_path, final_path) if path is not None),
        )
        raise
    return finalize_staged_3mf_submission(
        db,
        original_filename=original_filename,
        operation_id=operation_id,
        principal=principal,
        cost_center_id=cost_center_id,
        staging_path=staging_path,
        final_path=final_path,
        byte_count=total,
        digest=digest.hexdigest(),
    )
