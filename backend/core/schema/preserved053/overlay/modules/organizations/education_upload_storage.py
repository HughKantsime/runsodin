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
from core.economic_identity import _add_created
from modules.jobs.models import Job
from modules.organizations.education_storage import MIN_FREE_BYTES, StorageConfigurationError, required_free_bytes





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
    db: Session, *, operation_id: str, amount: int, storage_root: Path | None = None
) -> None:
    storage_root = owner.UPLOAD_ROOT if storage_root is None else storage_root
    if amount <= 0 or amount > owner.CHUNK_BYTES:
        raise ValueError("reservation amount must be one positive upload chunk")
    usage = shutil.disk_usage(storage_root)
    try:
        required_free = required_free_bytes(usage.total)
    except StorageConfigurationError as exc:
        raise OdinError(ErrorCode.validation_failed,
                        "Education upload storage configuration is invalid. "
                        "Ask your administrator to correct EDUCATION_MIN_FREE_GIB (minimum 1 GiB).",
                        status=503, retriable=False) from exc
    if usage.free - amount < required_free:
        raise OdinError(
            ErrorCode.quota_exceeded,
            ("Upload would violate the Education storage headroom reserve. "
             f"Available: {usage.free / (1024 ** 3):.2f} GiB; "
             f"required reserve: {required_free / (1024 ** 3):.2f} GiB. "
             "Ask your administrator to free space or expand the filesystem "
             "backing Education uploads, and confirm the upload volume is mounted correctly."),
            status=507,
            retriable=True,
        )
    operation = owner._operation(db, operation_id)
    if not operation or operation.state not in ("reserved", "streaming"):
        raise OdinError(ErrorCode.invalid_state_transition, "Upload is not streamable", status=409)
    org_id = int(operation.org_id)
    user_id = int(operation.user_id)
    owner._ensure_storage_account(
        db, org_id=org_id, scope_kind="user", scope_id=user_id, user_id=user_id
    )
    owner._ensure_storage_account(
        db, org_id=org_id, scope_kind="tenant", scope_id=org_id, user_id=None
    )
    for scope_kind, scope_id, cap in (
        ("user", user_id, owner.USER_STORAGE_CAP_BYTES),
        ("tenant", org_id, owner.TENANT_STORAGE_CAP_BYTES),
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
                owner._fsync_directory(path.parent)
        except OSError as exc:
            cleanup_error = exc
    operation = owner._operation(db, operation_id)
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
    owner._release_reservation(db, operation)
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
    operation = owner._operation(db, operation_id)
    if not operation or operation.state != "needs_reconciliation":
        row = owner._submission_result(db, operation_id)
        return dict(row._mapping) if row else None
    row = owner._submission_result(db, operation_id)
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
    owner.abort_upload(db, operation_id=operation_id, paths=paths)
    return None

# Import after definitions so both the public facade and this module can load first.
import modules.organizations.education_submission_service as owner
