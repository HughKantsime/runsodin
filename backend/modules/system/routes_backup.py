"""System backup/restore routes — create, list, download, delete, and restore database backups."""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role

log = logging.getLogger("odin.api")
router = APIRouter()


@router.post("/backups/restore", tags=["System"])
async def restore_backup(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Restore database from an uploaded backup file."""
    import sqlite3
    import tempfile

    if not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db files are supported")

    MAX_BACKUP_BYTES = 100 * 1024 * 1024  # 100 MB
    content = await file.read(MAX_BACKUP_BYTES + 1)
    if len(content) > MAX_BACKUP_BYTES:
        raise HTTPException(status_code=413, detail="Backup file too large")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        test_conn = sqlite3.connect(tmp_path)
        result = test_conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail="Backup file failed integrity check")
        tables = [r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "users" not in tables:
            test_conn.close()
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail="Backup file is not a valid O.D.I.N. database")
        triggers = test_conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        if triggers:
            test_conn.close()
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail="Backup contains unexpected database triggers")
        test_conn.close()
    except sqlite3.Error as e:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=f"Invalid database file: {e}")

    db_path = "/data/odin.db"
    backup_dir = "/data/backups"
    os.makedirs(backup_dir, exist_ok=True)
    pre_restore_name = f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(db_path, os.path.join(backup_dir, pre_restore_name))

    shutil.copy2(tmp_path, db_path)
    os.unlink(tmp_path)

    log_audit(db, "backup_restored", details=f"Restored from {file.filename}, pre-restore backup: {pre_restore_name}")

    return {
        "status": "ok",
        "message": "Database restored. Restart the container to apply changes.",
        "pre_restore_backup": pre_restore_name,
    }


@router.post("/backups", tags=["System"])
def create_backup(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Create a database backup using SQLite online backup API."""
    import sqlite3 as sqlite3_mod

    backup_dir = Path(__file__).parent.parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    engine_url = str(db.get_bind().url)
    if "///" in engine_url:
        db_path = engine_url.split("///", 1)[1]
    else:
        db_path = "odin.db"
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", db_path)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_name = f"odin_backup_{timestamp}.db"
    backup_path = str(backup_dir / backup_name)

    src = sqlite3_mod.connect(db_path)
    dst = sqlite3_mod.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()

    size = os.path.getsize(backup_path)
    log_audit(db, "backup_created", "system", details={"filename": backup_name, "size_bytes": size})

    return {
        "filename": backup_name,
        "size_bytes": size,
        "size_mb": round(size / 1048576, 2),
        "created_at": datetime.now(timezone.utc).isoformat()
    }


@router.get("/backups", tags=["System"])
def list_backups(current_user: dict = Depends(require_role("admin"))):
    """List all database backups."""
    backup_dir = Path(__file__).parent.parent / "backups"
    if not backup_dir.exists():
        return []

    backups = []
    for f in sorted(backup_dir.glob("odin_backup_*.db"), reverse=True):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / 1048576, 2),
            "created_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        })
    return backups


@router.get("/backups/{filename}", tags=["System"])
def download_backup(filename: str, current_user: dict = Depends(require_role("admin"))):
    """Download a database backup file."""
    backup_dir = os.path.realpath(str(Path(__file__).parent.parent / "backups"))
    backup_path = os.path.realpath(os.path.join(backup_dir, filename))
    if not backup_path.startswith(backup_dir + os.sep):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(
        path=backup_path,
        filename=filename,
        media_type="application/octet-stream"
    )


@router.delete("/backups/{filename}", status_code=status.HTTP_204_NO_CONTENT, tags=["System"])
def delete_backup(filename: str, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a database backup."""
    backup_dir = os.path.realpath(str(Path(__file__).parent.parent / "backups"))
    backup_path = os.path.realpath(os.path.join(backup_dir, filename))
    if not backup_path.startswith(backup_dir + os.sep):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not os.path.exists(backup_path):
        raise HTTPException(status_code=404, detail="Backup not found")

    os.unlink(backup_path)
    log_audit(db, "backup_deleted", "system", details={"filename": filename})
