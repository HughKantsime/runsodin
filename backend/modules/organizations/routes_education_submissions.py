"""HTTP boundary for durable Education submissions."""

import hashlib
import io
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from core.db import get_db
from core.errors import ErrorCode, OdinError
from modules.organizations.education_access import require_education_principal
from modules.organizations.education_submission_service import (
    CHUNK_BYTES,
    MAX_UPLOAD_BYTES,
    UPLOAD_ROOT,
    abort_upload,
    begin_upload_attempt,
    finalize_staged_3mf_submission,
    prepare_upload_paths,
    reserve_storage_chunk,
    validate_replay_identity,
)


router = APIRouter(prefix="/education/submissions", tags=["Education"])


class _ReservedUploadFile(UploadFile):
    """Hash every chunk and reserve retained storage before each disk write."""

    def __init__(self, *, db, operation_id, storage_root, retain, file, **kwargs):
        super().__init__(file=file, **kwargs)
        self.db = db
        self.operation_id = operation_id
        self.storage_root = storage_root
        self.retain = retain
        self.byte_count = 0
        self.digest = hashlib.sha256()

    async def write(self, data: bytes) -> None:
        for offset in range(0, len(data), CHUNK_BYTES):
            chunk = data[offset : offset + CHUNK_BYTES]
            if self.byte_count + len(chunk) > MAX_UPLOAD_BYTES:
                raise MultiPartException("File exceeded the 100 MiB Education upload limit")
            if self.retain:
                reserve_storage_chunk(
                    self.db,
                    operation_id=self.operation_id,
                    amount=len(chunk),
                    storage_root=self.storage_root,
                )
                self.file.write(chunk)
            self.digest.update(chunk)
            self.byte_count += len(chunk)
        self.size = self.byte_count

    async def seek(self, offset: int) -> None:
        if self.retain:
            self.file.flush()
            os.fsync(self.file.fileno())


class _DurableMultipartParser(MultiPartParser):
    """Admit before file bytes, then stream directly into reserved ODIN storage."""

    def __init__(self, *args, db, principal, storage_root=UPLOAD_ROOT, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = db
        self.principal = principal
        self.storage_root = storage_root
        self.operation_id: str | None = None
        self.cost_center_id: int | None = None
        self.staging_path: Path | None = None
        self.final_path: Path | None = None
        self.replay: dict | None = None
        self.upload: _ReservedUploadFile | None = None
        self.owns_operation = False

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        ordinary_upload = self._current_part.file
        if ordinary_upload is None:
            return
        if self._current_part.field_name != "file":
            raise MultiPartException("The only permitted file field is 'file'")
        fields = dict(self.items)
        token = str(fields.get("submission_token") or "")
        raw_center = str(fields.get("cost_center_id") or "")
        if not token or not raw_center:
            raise MultiPartException(
                "submission_token and cost_center_id must precede the file field"
            )
        try:
            center_id = int(raw_center)
            if center_id <= 0:
                raise ValueError
        except ValueError as exc:
            raise MultiPartException("cost_center_id must be a positive integer") from exc
        self.operation_id = token
        self.cost_center_id = center_id
        self.replay = begin_upload_attempt(
            self.db,
            operation_id=token,
            principal=self.principal,
            cost_center_id=center_id,
        )
        retain = self.replay is None
        self.owns_operation = retain
        if retain:
            self.staging_path, self.final_path = prepare_upload_paths(
                self.db,
                operation_id=token,
                principal=self.principal,
                original_filename=ordinary_upload.filename or "",
                storage_root=self.storage_root,
            )
            destination = open(self.staging_path, "xb")
        else:
            destination = io.BytesIO()
        ordinary_upload.file.close()
        self.upload = _ReservedUploadFile(
            db=self.db,
            operation_id=token,
            storage_root=self.storage_root,
            retain=retain,
            file=destination,
            size=0,
            filename=ordinary_upload.filename,
            headers=ordinary_upload.headers,
        )
        self._current_part.file = self.upload
        self._files_to_close_on_error.append(destination)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_education_submission(
    request: Request,
    principal: dict = Depends(require_education_principal(write=True)),
    db: Session = Depends(get_db),
):
    """Stream one sliced 3MF into a durable, reviewable Education submission."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                raise OdinError(
                    ErrorCode.quota_exceeded,
                    "Multipart upload exceeds the Education upload limit",
                    status=413,
                )
        except ValueError as exc:
            raise OdinError(
                ErrorCode.validation_failed, "Invalid Content-Length", status=400
            ) from exc
    parser = _DurableMultipartParser(
        request.headers,
        request.stream(),
        db=db,
        principal=principal,
        max_files=1,
        max_fields=2,
        max_part_size=1024,
    )
    try:
        try:
            await parser.parse()
        except MultiPartException as exc:
            raise OdinError(ErrorCode.validation_failed, str(exc), status=400) from exc
        upload = parser.upload
        if upload is None or parser.operation_id is None or parser.cost_center_id is None:
            raise OdinError(
                ErrorCode.validation_failed,
                "Multipart field 'file' is required",
                status=422,
                extra={"fields": ["file"]},
            )
        await upload.close()
        digest = upload.digest.hexdigest()
        if parser.replay is not None:
            validate_replay_identity(
                db,
                operation_id=parser.operation_id,
                byte_count=upload.byte_count,
                digest=digest,
            )
            return parser.replay
        return finalize_staged_3mf_submission(
            db,
            original_filename=upload.filename or "",
            operation_id=parser.operation_id,
            principal=principal,
            cost_center_id=parser.cost_center_id,
            staging_path=parser.staging_path,
            final_path=parser.final_path,
            byte_count=upload.byte_count,
            digest=digest,
        )
    finally:
        if parser.upload is not None and not parser.upload.file.closed:
            await parser.upload.close()
        if parser.operation_id and parser.owns_operation:
            operation_paths = tuple(
                path for path in (parser.staging_path, parser.final_path) if path is not None
            )
            operation = db.execute(
                text("SELECT state FROM education_upload_operations WHERE operation_id=:id"),
                {"id": parser.operation_id},
            ).fetchone()
            if operation and operation.state not in ("committed", "needs_reconciliation"):
                abort_upload(db, operation_id=parser.operation_id, paths=operation_paths)
