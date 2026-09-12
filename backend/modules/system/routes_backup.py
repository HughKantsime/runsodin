"""System backup routes with online creation and offline restore staging."""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.config import settings
from core.db import get_db
from core.db_compat import sql
from core.dependencies import log_audit
from core.rbac import require_superadmin
from modules.system.backup_service import (
    MAX_BACKUP_BYTES,
    BackupValidationError,
    create_online_backup,
    paths_from_database_url,
    stage_restore,
)

router = APIRouter()


def _sqlite_paths():
    if sql.is_postgres:
        raise HTTPException(
            status_code=501,
            detail="SQLite backup is unavailable for PostgreSQL; use pg_dump/pg_restore.",
        )
    try:
        return paths_from_database_url(settings.database_url)
    except BackupValidationError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@router.post("/backups/restore", tags=["System"])
async def restore_backup(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db),
):
    """Validate and stage a database restore for the next process start."""
    _sqlite_paths()
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db files are supported")
    content = await file.read(MAX_BACKUP_BYTES + 1)
    if len(content) > MAX_BACKUP_BYTES:
        raise HTTPException(status_code=413, detail="Backup file too large")
    descriptor, temporary_name = tempfile.mkstemp(prefix="odin-restore-upload-", suffix=".db")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        result = stage_restore(
            temporary,
            settings.database_url,
            actor_id=current_user.get("id"),
        )
    except BackupValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "staged",
        "message": "Restore validated and staged. Restart ODIN to apply it offline.",
        "restart_required": True,
        "pre_restore_backup": result["pre_restore_backup"],
    }


@router.post("/backups", tags=["System"])
def create_backup(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Create and verify a restrictive SQLite online backup."""
    _sqlite_paths()
    try:
        path, metadata = create_online_backup(settings.database_url)
    except BackupValidationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    log_audit(
        db,
        "backup_created",
        "system",
        details={"filename": path.name, "size_bytes": metadata["size_bytes"]},
    )
    db.commit()
    return {
        "filename": path.name,
        "size_bytes": metadata["size_bytes"],
        "size_mb": round(int(metadata["size_bytes"]) / 1048576, 2),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/backups", tags=["System"])
def list_backups(current_user: dict = Depends(require_superadmin())):
    paths = _sqlite_paths()
    if not paths.backups.exists():
        return []
    return [
        {
            "filename": item.name,
            "size_bytes": item.stat().st_size,
            "size_mb": round(item.stat().st_size / 1048576, 2),
            "created_at": datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc).isoformat(),
        }
        for item in sorted(paths.backups.glob("odin_backup_*.db"), reverse=True)
    ]


def _safe_backup_path(filename: str) -> Path:
    paths = _sqlite_paths()
    candidate = (paths.backups / filename).resolve()
    if (
        candidate.parent != paths.backups.resolve()
        or not filename.startswith("odin_backup_")
        or candidate.suffix != ".db"
    ):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    return candidate


@router.get("/backups/{filename}", tags=["System"])
def download_backup(filename: str, current_user: dict = Depends(require_superadmin())):
    path = _safe_backup_path(filename)
    return FileResponse(path=path, filename=path.name, media_type="application/octet-stream")


@router.delete("/backups/{filename}", status_code=status.HTTP_204_NO_CONTENT, tags=["System"])
def delete_backup(
    filename: str,
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db),
):
    path = _safe_backup_path(filename)
    path.unlink()
    log_audit(db, "backup_deleted", "system", details={"filename": path.name})
    db.commit()
