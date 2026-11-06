"""Durable streamed upload state machine for Education submissions."""

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
from core.economic_identity import _add_created
from modules.jobs.models import Job
from modules.organizations.education_storage import MIN_FREE_BYTES, StorageConfigurationError, required_free_bytes


def _analyze_3mf_isolated(path, ticket):
    """(status, metadata) from the bounded 3MF child process (never this process); 429
    with Retry-After when the install's 3MF workers are busy. Blocking: callers run it
    on the 3MF executor (see routes_education_submissions)."""
    from fastapi import HTTPException
    from modules.models_library.threemf_parser import ThreeMFBusy, analyze_education_3mf, reserve_3mf_analysis
    try:
        return analyze_education_3mf(path, ticket if ticket is not None else reserve_3mf_analysis())
    except ThreeMFBusy as busy:
        raise HTTPException(status_code=429, detail="3MF analysis is busy; try again shortly",
                            headers={"Retry-After": str(busy.retry_after)}) from None

CHUNK_BYTES = 1024 * 1024
from core.print_upload_limits import MAX_UPLOAD_BYTES, size_refusal
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
USER_STORAGE_CAP_BYTES = 2 * 1024 * 1024 * 1024
TENANT_STORAGE_CAP_BYTES = 50 * 1024 * 1024 * 1024
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
    from modules.organizations.education_training_policy import lock_center
    lock_center(db, cost_center_id, org_id)
    retain_membership(db, principal["id"], org_id)
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

    from modules.organizations.education_training_policy import require_submission_training
    require_submission_training(db, principal=principal, cost_center_id=cost_center_id)

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
                ErrorCode.quota_exceeded, size_refusal(total), status=413
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
    from core.controlled_education import assert_unmarked_upload_path
    assert_unmarked_upload_path(db, final_path)
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
    threemf_ticket=None,
) -> dict:
    """Validate a fully reserved staging file and atomically commit its graph.

    Blocking (database work and the isolated 3MF analysis): async callers run it on
    the 3MF executor with a ``threemf_ticket`` taken on the event loop."""
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
        # The package is opened and parsed only by the bounded 3MF child process.
        status, metadata = _analyze_3mf_isolated(staging_path, threemf_ticket)
        if status == "too_large":
            raise OdinError(ErrorCode.validation_failed, "Expanded .3mf exceeds 500 MiB", status=400)
        if status == "plate_json_too_large":
            raise OdinError(ErrorCode.validation_failed, "A plate description in the .3mf exceeds 4 MB", status=400)
        if status in ("bad_zip", "too_many_members"):
            raise OdinError(ErrorCode.validation_failed, "Invalid .3mf container", status=400)
        if status == "no_plate":
            # Bambu dispatch selects this exact first-plate path. A suffix
            # match, alternate plate, or empty member cannot be printed.
            raise OdinError(
                ErrorCode.validation_failed,
                "Education upload requires one nonempty Metadata/plate_1.gcode member",
                status=400,
            )
        if not metadata or metadata.print_time_seconds <= 0:
            raise OdinError(ErrorCode.validation_failed, "Education upload must be a sliced .3mf", status=400)
        compatibility_meta = _compatibility_meta_from_parsed(metadata)
        from core.controlled_education import assert_unmarked_upload_path
        assert_unmarked_upload_path(db, final_path)
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
                        size_refusal(total + len(chunk)),
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

from modules.organizations.education_upload_storage import (
    _ensure_storage_account,
    reserve_storage_chunk,
    _release_reservation,
    abort_upload,
    reconcile_upload_operation,
)

from modules.organizations.education_submission_graph import (
    _lock_job_birth_authority,
    _raw_insert_job_defaults,
    _commit_submission_graph,
)
