"""System admin routes — live log viewer, support bundle, and global search."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from modules.models_library.models import Model
from modules.jobs.models import Job
from modules.inventory.models import Spool, FilamentLibrary
from modules.printers.models import Printer

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Live Log Viewer ==============

_LOG_FILES = {
    "backend": "/data/backend.log",
    "mqtt": "/data/mqtt_monitor.log",
    "moonraker": "/data/moonraker_monitor.log",
    "prusalink": "/data/prusalink_monitor.log",
    "elegoo": "/data/elegoo_monitor.log",
    "vision": "/data/vision_monitor.log",
    "go2rtc": "/data/go2rtc.log",
    "timelapse": "/data/timelapse_capture.log",
    "reports": "/data/report_runner.log",
}


@router.get("/admin/logs", tags=["Admin"])
def get_logs(
    source: str = Query("backend", description="Log source: backend, mqtt, moonraker, prusalink, elegoo, vision, go2rtc, timelapse, reports"),
    lines: int = Query(200, ge=1, le=5000),
    level: Optional[str] = Query(None, description="Filter by log level: DEBUG, INFO, WARNING, ERROR"),
    search: Optional[str] = Query(None, description="Text filter"),
    user=Depends(require_role("admin")),
):
    """Return the last N lines from a log file, optionally filtered by level/text."""
    log_path = _LOG_FILES.get(source)
    if not log_path or not os.path.isfile(log_path):
        return {"lines": [], "source": source}

    import collections
    result = collections.deque(maxlen=lines)
    level_upper = level.upper() if level else None

    with open(log_path, "r", errors="replace") as f:
        for line in f:
            stripped = line.rstrip("\n")
            if level_upper and level_upper not in stripped.upper():
                continue
            if search and search.lower() not in stripped.lower():
                continue
            result.append(stripped)

    return {"lines": list(result), "source": source}


@router.get("/admin/logs/stream", tags=["Admin"])
async def stream_logs(
    request: Request,
    source: str = Query("backend"),
    level: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    user=Depends(require_role("admin")),
):
    """SSE endpoint — streams new log lines in real time."""
    import asyncio

    log_path = _LOG_FILES.get(source)
    if not log_path or not os.path.isfile(log_path):
        raise HTTPException(status_code=404, detail="Log source not found")

    level_upper = level.upper() if level else None

    async def event_generator():
        with open(log_path, "r", errors="replace") as f:
            f.seek(0, 2)
            while True:
                if await request.is_disconnected():
                    break
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue
                stripped = line.rstrip("\n")
                if level_upper and level_upper not in stripped.upper():
                    continue
                if search and search.lower() not in stripped.lower():
                    continue
                yield f"data: {json.dumps(stripped)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============== Support Bundle ==============

@router.get("/admin/support-bundle", tags=["Admin"])
def download_support_bundle(user=Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Generate a privacy-filtered diagnostic ZIP for issue reporting."""
    import collections
    import io
    import platform
    import sqlite3
    import subprocess
    import zipfile
    from pathlib import Path

    buf = io.BytesIO()
    now = datetime.now(timezone.utc).isoformat()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        db_path = "/data/odin.db"
        db_size = os.path.getsize(db_path) if os.path.isfile(db_path) else 0
        printer_count = db.execute(text("SELECT COUNT(*) FROM printers")).scalar() or 0
        user_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
        job_count = db.execute(text("SELECT COUNT(*) FROM print_jobs")).scalar() or 0

        docker_version = "unknown"
        try:
            docker_version = subprocess.check_output(["docker", "--version"], timeout=5, text=True).strip()
        except Exception:
            pass

        _version_file = Path(__file__).parent.parent.parent / "VERSION"
        odin_version = _version_file.read_text().strip() if _version_file.exists() else "unknown"

        zf.writestr("system_info.json", json.dumps({
            "os": platform.platform(),
            "python_version": platform.python_version(),
            "docker_version": docker_version,
            "odin_version": odin_version,
            "db_size_bytes": db_size,
            "printer_count": printer_count,
            "user_count": user_count,
            "job_count": job_count,
        }, indent=2))

        rows = db.execute(text(
            "SELECT id, name, api_type, gcode_state, last_seen FROM printers"
        )).fetchall()
        printers_info = []
        for r in rows:
            printers_info.append({
                "id": r[0], "name": r[1], "protocol": r[2],
                "state": r[3], "last_seen": r[4],
            })
        zf.writestr("connectivity.json", json.dumps(printers_info, indent=2, default=str))

        config_rows = db.execute(text("SELECT key, value FROM system_config")).fetchall()
        safe_config = {}
        sensitive = ("password", "key", "token", "secret", "code", "credential")
        for k, v in config_rows:
            if any(s in k.lower() for s in sensitive):
                safe_config[k] = "[REDACTED]"
            else:
                safe_config[k] = v
        zf.writestr("settings_safe.json", json.dumps(safe_config, indent=2))

        log_path = "/data/backend.log"
        error_lines = collections.deque(maxlen=100)
        if os.path.isfile(log_path):
            with open(log_path, "r", errors="replace") as f:
                for line in f:
                    if "WARNING" in line or "ERROR" in line:
                        error_lines.append(line.rstrip("\n"))
        zf.writestr("recent_errors.txt", "\n".join(error_lines))

        health = {}
        try:
            conn = sqlite3.connect(db_path)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            health["integrity"] = integrity[0] if integrity else "unknown"
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            counts = {}
            for (tname,) in tables:
                try:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM [{tname}]").fetchone()
                    counts[tname] = cnt[0] if cnt else 0
                except Exception:
                    counts[tname] = "error"
            health["table_row_counts"] = counts
            conn.close()
        except Exception as e:
            health["error"] = str(e)
        zf.writestr("db_health.json", json.dumps(health, indent=2))

        zf.writestr("bundle_metadata.json", json.dumps({
            "generated_at": now,
            "odin_version": odin_version,
            "python_version": platform.python_version(),
        }, indent=2))

    buf.seek(0)
    filename = f"odin-support-bundle-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ============== Global Search ==============

@router.get("/search", tags=["Search"])
def global_search(q: str = "", db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer"))):
    """Search across models, jobs, spools, and printers."""
    if not q or len(q) < 2:
        return {"models": [], "jobs": [], "spools": [], "printers": []}
    if len(q) > 200:
        return {"models": [], "jobs": [], "spools": [], "printers": []}

    query = f"%{q.lower()}%"

    models = db.query(Model).filter(
        (Model.name.ilike(query)) | (Model.notes.ilike(query))
    ).limit(5).all()

    jobs = db.query(Job).filter(
        (Job.item_name.ilike(query)) | (Job.notes.ilike(query))
    ).order_by(Job.created_at.desc()).limit(5).all()

    spools = db.query(Spool).outerjoin(FilamentLibrary, Spool.filament_id == FilamentLibrary.id).filter(
        (Spool.qr_code.ilike(query)) |
        (Spool.vendor.ilike(query)) |
        (Spool.notes.ilike(query)) |
        (FilamentLibrary.brand.ilike(query)) |
        (FilamentLibrary.name.ilike(query)) |
        (FilamentLibrary.material.ilike(query))
    ).limit(5).all()

    printers = db.query(Printer).filter(
        Printer.name.ilike(query)
    ).limit(5).all()

    return {
        "models": [{"id": m.id, "name": m.name, "type": "model"} for m in models],
        "jobs": [{"id": j.id, "name": j.item_name, "status": j.status.value if j.status else None, "type": "job"} for j in jobs],
        "spools": [{"id": s.id, "name": f"{s.filament.brand} {s.filament.name}" if s.filament else (s.vendor or f"Spool #{s.id}"), "qr_code": s.qr_code, "type": "spool"} for s in spools],
        "printers": [{"id": p.id, "name": p.name, "type": "printer"} for p in printers],
    }
