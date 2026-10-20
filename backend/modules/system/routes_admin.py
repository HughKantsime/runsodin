"""System admin routes — live log viewer, support bundle, and global search."""

from core.rbac import should_filter_org
from modules.system.sanitize import sanitize_log_lines, sanitize_log_line

import json
import collections
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import sql
from core.rbac import require_role, require_superadmin, get_org_scope, HANDLER_SCOPE, require_interactive_session
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
    "anycubic_lan": "/data/anycubic_lan_monitor.log",
    "vision": "/data/vision_monitor.log",
    "go2rtc": "/data/go2rtc.log",
    "timelapse": "/data/timelapse_capture.log",
    "reports": "/data/report_runner.log",
    "supervisord": "/data/supervisord.log",
}


# Bound each returned line even when a process writes without newlines.
_MAX_LOG_LINE_BYTES = 16384

# The level of a log line is its first level token (Python logging "LEVEL" or
# "[LEVEL]", supervisord, zerolog's INF/WRN/ERR/DBG). Matching the token, not a
# substring of the whole line, keeps "[INFO] ... transient error" out of ERROR.
_LEVEL_TOKEN = re.compile(r"\b(DEBUG|DBG|INFO|INF|WARNING|WARN|WRN|ERROR|ERR|CRITICAL|CRIT|FATAL|FTL)\b")
_LEVEL_ALIASES = {"DBG": "DEBUG", "INF": "INFO", "WARN": "WARNING", "WRN": "WARNING", "ERR": "ERROR",
                  "CRITICAL": "ERROR", "CRIT": "ERROR", "FATAL": "ERROR", "FTL": "ERROR"}


def _line_level(line: str) -> Optional[str]:
    if line[:1].isspace():
        return None  # traceback/continuation line: it carries no level of its own
    match = _LEVEL_TOKEN.search(line)
    return _LEVEL_ALIASES.get(match.group(1), match.group(1)) if match else None


@router.get("/admin/logs", tags=["Admin"], dependencies=[Depends(require_interactive_session())])
def get_logs(
    source: str = Query("backend", description="Configured application log source"),
    lines: int = Query(200, ge=1, le=5000),
    level: Optional[str] = Query(None, description="Filter by log level: DEBUG, INFO, WARNING, ERROR"),
    search: Optional[str] = Query(None, description="Text filter"),
    cursor: Optional[str] = Query(None, max_length=128, description="Opaque older-page cursor returned by this endpoint"),
    user=Depends(require_superadmin()),
):
    """Return a bounded newest-first page selection, with lines in file order.

    Default calls retain tail-N behavior. A cursor pins the snapshot and oldest
    shown byte boundary so appends do not move subsequent older pages.
    """
    log_path = _LOG_FILES.get(source)
    if not log_path or not os.path.isfile(log_path):
        return {"lines": [], "source": source, "next_cursor": None, "page_cursor": None}

    level_upper = level.upper() if level else None
    result = collections.deque(maxlen=lines + 1)
    with open(log_path, "rb") as log_file:
        stat = os.fstat(log_file.fileno())
        snapshot = boundary = stat.st_size
        if cursor:
            try:
                parts = cursor.split(":")
                if len(parts) != 4 or not all(part.isascii() and part.isdigit() for part in parts):
                    raise ValueError()
                device, inode, snapshot, boundary = map(int, parts)
                if not 0 <= boundary <= snapshot:
                    raise ValueError()
            except (ValueError, OverflowError):
                raise HTTPException(400, "Invalid log pagination cursor")
            if (device, inode) != (stat.st_dev, stat.st_ino) or snapshot > stat.st_size:
                raise HTTPException(409, "Log file changed. Refresh to load the latest logs.")

        while log_file.tell() < boundary:
            line_start = log_file.tell()
            chunk = log_file.readline(min(_MAX_LOG_LINE_BYTES, boundary - line_start))
            if not chunk:
                break
            truncated = not chunk.endswith(b"\n") and log_file.tell() < boundary
            # Discard the remainder in bounded chunks while preserving one entry
            # and its byte boundary per physical line.
            last = chunk
            while not last.endswith(b"\n") and log_file.tell() < boundary:
                last = log_file.readline(min(_MAX_LOG_LINE_BYTES, boundary - log_file.tell()))
                if not last:
                    break
            stripped = chunk.rstrip(b"\r\n").decode("utf-8", errors="replace")
            if truncated:
                stripped += " [line truncated]"
            # Filter what the caller would see: searching raw lines would confirm redacted secrets.
            shown = sanitize_log_line(stripped)
            if level_upper and _line_level(shown) != level_upper:
                continue
            if search and search.lower() not in shown.lower():
                continue
            result.append((line_start, shown))

        page = list(result)
        has_older = len(page) > lines
        if has_older:
            page = page[-lines:]
        next_cursor = f"{stat.st_dev}:{stat.st_ino}:{snapshot}:{page[0][0]}" if has_older else None
    return {"lines": [line for _, line in page], "source": source, "next_cursor": next_cursor,
            "page_cursor": f"{stat.st_dev}:{stat.st_ino}:{snapshot}:{boundary}"}


@router.get("/admin/logs/stream", tags=["Admin"], dependencies=[Depends(require_interactive_session())])
async def stream_logs(
    request: Request,
    source: str = Query("backend"),
    level: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    user=Depends(require_superadmin()),
):
    """SSE endpoint — streams new log lines in real time. Superadmin only."""
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
                shown = sanitize_log_line(line.rstrip("\n"))  # filter what the caller would see
                if level_upper and _line_level(shown) != level_upper:
                    continue
                if search and search.lower() not in shown.lower():
                    continue
                yield f"data: {json.dumps(shown)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============== Support Bundle ==============

@router.get("/admin/support-bundle", tags=["Admin"], dependencies=[Depends(require_interactive_session())])
def download_support_bundle(user=Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Generate a privacy-filtered diagnostic ZIP for issue reporting. Superadmin only."""
    import io
    import platform
    import sqlite3
    import subprocess
    import zipfile
    from pathlib import Path

    buf = io.BytesIO()
    now = datetime.now(timezone.utc).isoformat()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # The configured database (DATABASE_URL), not a hard-coded Docker path.
        db_path = db.get_bind().url.database if sql.is_sqlite else None
        db_size = os.path.getsize(db_path) if db_path and os.path.isfile(db_path) else 0
        printer_count = db.execute(text("SELECT COUNT(*) FROM printers")).scalar() or 0
        user_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0
        job_count = db.execute(text("SELECT COUNT(*) FROM print_jobs")).scalar() or 0

        docker_version = "unknown"
        try:
            docker_version = subprocess.check_output(["docker", "--version"], timeout=5, text=True).strip()
        except Exception as e:
            log.debug(f"Failed to get docker version: {e}")

        _version_file = Path(__file__).parent.parent.parent.parent / "VERSION"
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
        # Internal bookkeeping rows (one-time markers, per-print quota claims) are
        # not configuration and never belong in a shared diagnostics bundle.
        internal_prefixes = ("quota_attempt:", "quota_charged:", "setup_admin_claimed",
                             "biometric_revoked", "square_readonly:", "show_fulfillment:", "production_finishing:", "production_operations:", "material_purchases:", "material_identity:", "material_mutex:")
        for k, v in config_rows:
            if any(k.startswith(p) for p in internal_prefixes):
                continue
            if any(s in k.lower() for s in sensitive) or k in ("smtp_config", "base_url"):
                safe_config[k] = "[REDACTED]"
            else:
                safe_config[k] = sanitize_log_line(str(v))
        zf.writestr("settings_safe.json", json.dumps(safe_config, indent=2))

        from modules.system.diagnostics import _read_tail
        recent = _read_tail('/data/backend.log', 100)
        error_lines = [line for line in recent if 'WARNING' in line or 'ERROR' in line] if isinstance(recent, list) else []
        zf.writestr("recent_errors.txt", "\n".join(sanitize_log_lines(error_lines)))

        health = {}
        if sql.is_sqlite:
            try:
                if not db_path or not os.path.isfile(db_path):
                    raise FileNotFoundError("configured SQLite database file not found")
                # Read-only: diagnostics must never create or modify the database.
                conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                health["integrity"] = integrity[0] if integrity else "unknown"
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                counts = {}
                for (tname,) in tables:
                    try:
                        cnt = conn.execute(f"SELECT COUNT(*) FROM [{tname}]").fetchone()  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- verified safe — sql.* helpers return constant SQL fragments, no user input
                        counts[tname] = cnt[0] if cnt else 0
                    except Exception:
                        counts[tname] = "error"
                health["table_row_counts"] = counts
                conn.close()
            except Exception as e:
                health["error"] = str(e)
        else:
            health["integrity"] = "n/a (PostgreSQL)"
            health["note"] = "Use pg_dump or PostgreSQL admin tools for DB health checks"
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
def global_search(q: str = "", db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope=HANDLER_SCOPE))):
    """Search tenant-visible resources within each category's read grants."""
    from core.api_token_policy import token_scope_granted
    from modules.orders.models import Order, Product
    from sqlalchemy import false

    scopes = current_user.get("_token_scopes", [])
    scoped = current_user.get("_auth_kind") == "user_api_token" or bool(scopes)
    def allowed(*grants):
        return not scoped or any(token_scope_granted(scopes, grant) for grant in grants)
    empty = {key: [] for key in ("models", "jobs", "spools", "printers", "orders", "products")}
    if not q or len(q) < 2:
        return empty
    if len(q) > 200:
        return empty

    org = get_org_scope(current_user)
    query = f"%{q.lower()}%"

    mq = db.query(Model).filter((Model.name.ilike(query)) | (Model.notes.ilike(query)))
    if should_filter_org(current_user, org):
        mq = mq.filter((Model.org_id == org) | (Model.org_id == None))
    models = mq.filter(false() if not allowed("read:models") else True).limit(5).all()

    jq = db.query(Job).filter((Job.item_name.ilike(query)) | (Job.notes.ilike(query)))
    if should_filter_org(current_user, org):
        jq = jq.filter((Job.charged_to_org_id == org) | (Job.charged_to_org_id == None))
    jq = jq.filter(false() if not allowed("agent:read", "agent:write", "read:jobs") else True)
    jobs = jq.order_by(Job.created_at.desc()).limit(5).all()

    sq = db.query(Spool).outerjoin(FilamentLibrary, Spool.filament_id == FilamentLibrary.id).filter(
        (Spool.qr_code.ilike(query)) |
        (Spool.vendor.ilike(query)) |
        (Spool.notes.ilike(query)) |
        (FilamentLibrary.brand.ilike(query)) |
        (FilamentLibrary.name.ilike(query)) |
        (FilamentLibrary.material.ilike(query))
    )
    if should_filter_org(current_user, org):
        sq = sq.filter((Spool.org_id == org) | (Spool.org_id == None))
    spools = sq.filter(false() if not allowed("agent:read", "agent:write", "read:spools") else True).limit(5).all()

    pq = db.query(Printer).filter(Printer.name.ilike(query))
    if should_filter_org(current_user, org):
        pq = pq.filter((Printer.org_id == org) | (Printer.org_id == None) | (Printer.shared == True))
    printers = pq.filter(false() if not allowed("agent:read", "agent:write", "read:printers") else True).limit(5).all()

    oq = db.query(Order).filter((Order.order_number.ilike(query)) | (Order.customer_name.ilike(query)))
    cq = db.query(Product).filter((Product.name.ilike(query)) | (Product.sku.ilike(query)))
    if should_filter_org(current_user, org):
        oq = oq.filter((Order.org_id == org) | (Order.org_id == None))
        cq = cq.filter((Product.org_id == org) | (Product.org_id == None))
    orders = oq.filter(false() if not allowed("agent:read", "agent:write") else True).order_by(Order.created_at.desc()).limit(5).all()
    products = cq.filter(false() if not allowed("read") else True).order_by(Product.name).limit(5).all()

    return {
        "orders": [{"id": o.id, "name": o.order_number or f"Order #{o.id}", "type": "order", "status": o.status.value if o.status else None} for o in orders],
        "products": [{"id": p.id, "name": p.name, "type": "product"} for p in products],
        "models": [{"id": m.id, "name": m.name, "type": "model"} for m in models],
        "jobs": [{"id": j.id, "name": j.item_name, "status": j.status.value if j.status else None, "type": "job"} for j in jobs],
        "spools": [{"id": s.id, "name": f"{s.filament.brand} {s.filament.name}" if s.filament else (s.vendor or f"Spool #{s.id}"), "qr_code": s.qr_code, "type": "spool"} for s in spools],
        "printers": [{"id": p.id, "name": p.name, "type": "printer"} for p in printers],
    }
