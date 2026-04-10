# modules/system/diagnostics.py — Admin-only diagnostics endpoint
#
# Returns a comprehensive JSON bundle for troubleshooting.
# Every section is wrapped in try/except so a failure in one section
# never crashes the endpoint.

import logging
import os
import platform
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text

from core.rbac import require_role

log = logging.getLogger("odin.api")

router = APIRouter(tags=["System"])

_PROCESS_START = time.time()

_VERSION_FILE = Path(__file__).parent.parent.parent.parent / "VERSION"


def _read_version() -> str:
    try:
        if _VERSION_FILE.exists():
            return _VERSION_FILE.read_text().strip()
    except Exception:
        pass
    return "unknown"


def _read_tail(filepath: str, max_lines: int) -> list[str] | str:
    """Read last N lines from a file. Return 'not found' if file doesn't exist."""
    p = Path(filepath)
    if not p.exists():
        return "not found"
    try:
        lines = p.read_text(errors="replace").splitlines()
        return lines[-max_lines:]
    except Exception as e:
        return f"error: {e}"


def _system_info() -> dict:
    """Memory and disk info without psutil."""
    info = {}

    # Memory via /proc/meminfo (Linux only)
    try:
        meminfo_path = Path("/proc/meminfo")
        if meminfo_path.exists():
            content = meminfo_path.read_text()
            mem = {}
            for line in content.splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]  # value in kB
                    if key in ("MemTotal", "MemAvailable", "MemFree"):
                        mem[key.lower()] = int(val) * 1024  # convert to bytes
            info["memory"] = mem
        else:
            info["memory"] = "not available"
    except Exception as e:
        info["memory"] = f"error: {e}"

    # Disk via os.statvfs
    try:
        st = os.statvfs("/data")
        info["disk"] = {
            "total_bytes": st.f_frsize * st.f_blocks,
            "free_bytes": st.f_frsize * st.f_bavail,
            "used_bytes": st.f_frsize * (st.f_blocks - st.f_bavail),
        }
    except Exception:
        try:
            st = os.statvfs("/")
            info["disk"] = {
                "total_bytes": st.f_frsize * st.f_blocks,
                "free_bytes": st.f_frsize * st.f_bavail,
                "used_bytes": st.f_frsize * (st.f_blocks - st.f_bavail),
            }
        except Exception as e:
            info["disk"] = f"error: {e}"

    return info


def _printer_stats() -> dict:
    """Total printer count and count by api_type (protocol)."""
    from core.db import SessionLocal
    try:
        db = SessionLocal()
        try:
            total = db.execute(text("SELECT COUNT(*) FROM printers")).scalar()
            rows = db.execute(
                text("SELECT api_type, COUNT(*) as cnt FROM printers GROUP BY api_type")
            ).fetchall()
            by_protocol = {row[0]: row[1] for row in rows}
            return {"total": total, "by_protocol": by_protocol}
        finally:
            db.close()
    except Exception as e:
        return {"error": str(e)}


def _module_ids() -> list[str] | dict:
    """List registered module IDs from the registry."""
    try:
        from core.registry import registry
        return list(registry.providers.keys())
    except Exception as e:
        return {"error": str(e)}


def _supervisor_status() -> dict | str:
    """Parse supervisord.log for process states."""
    log_path = "/data/supervisord.log"
    raw = _read_tail(log_path, 50)
    if isinstance(raw, str):
        return raw  # "not found" or "error: ..."

    states = {}
    # Match lines like: INFO exited: process_name (exit status 0; expected)
    # or: INFO success: process_name entered RUNNING state
    # or: INFO spawned: 'process_name' with pid 123
    for line in raw:
        # RUNNING
        m = re.search(r"entered RUNNING state.*?(\S+)", line)
        if m:
            # Extract process name from earlier in line
            pm = re.search(r"success:\s+(\S+)", line)
            if pm:
                states[pm.group(1)] = "RUNNING"
            continue
        # STOPPED / FATAL
        for state in ("STOPPED", "FATAL"):
            if state in line:
                pm = re.search(r"(?:exited|stopped|fatal):\s+(\S+)", line)
                if pm:
                    states[pm.group(1)] = state

    return {"processes": states, "raw_lines": len(raw)}


def _license_tier() -> str | dict:
    """Return tier name only — no key or payload."""
    try:
        from license_manager import get_license
        info = get_license()
        return info.tier
    except Exception as e:
        return {"error": str(e)}


def _database_info() -> dict:
    """Table row counts, DB file size, WAL size."""
    from core.db import SessionLocal
    try:
        db = SessionLocal()
        try:
            # Get all table names
            tables_raw = db.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            ).fetchall()
            table_counts = {}
            for (tbl,) in tables_raw:
                try:
                    count = db.execute(text(f"SELECT COUNT(*) FROM [{tbl}]")).scalar()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                    table_counts[tbl] = count
                except Exception:
                    table_counts[tbl] = "error"

            result = {"tables": table_counts}

            # DB file size
            db_path = Path("/data/odin.db")
            if db_path.exists():
                result["db_size_bytes"] = db_path.stat().st_size
            else:
                result["db_size_bytes"] = "not found"

            # WAL size
            wal_path = Path("/data/odin.db-wal")
            if wal_path.exists():
                result["wal_size_bytes"] = wal_path.stat().st_size
            else:
                result["wal_size_bytes"] = "not found"

            return result
        finally:
            db.close()
    except Exception as e:
        return {"error": str(e)}


@router.get("/system/diagnostics")
async def get_diagnostics(current_user: dict = Depends(require_role("admin"))):
    """Admin-only diagnostics bundle for troubleshooting."""
    from modules.system.sanitize import sanitize_log_lines
    from core.error_buffer import error_buffer

    result = {}

    # Version
    try:
        result["odin_version"] = _read_version()
    except Exception as e:
        result["odin_version"] = {"error": str(e)}

    # Python / platform
    try:
        import sys
        result["python_version"] = sys.version
        result["platform"] = platform.platform()
        result["architecture"] = platform.machine()
    except Exception as e:
        result["python_info"] = {"error": str(e)}

    # Docker detection
    try:
        result["docker"] = (
            Path("/.dockerenv").exists()
            or os.environ.get("DOCKER") == "1"
        )
    except Exception as e:
        result["docker"] = {"error": str(e)}

    # Uptime
    try:
        result["uptime_seconds"] = round(time.time() - _PROCESS_START, 1)
    except Exception as e:
        result["uptime_seconds"] = {"error": str(e)}

    # Recent logs (sanitized)
    try:
        raw_logs = _read_tail("/data/backend.log", 100)
        if isinstance(raw_logs, list):
            result["recent_logs"] = sanitize_log_lines(raw_logs)
        else:
            result["recent_logs"] = raw_logs  # "not found"
    except Exception as e:
        result["recent_logs"] = {"error": str(e)}

    # Error buffer
    try:
        result["error_buffer"] = error_buffer.entries()
    except Exception as e:
        result["error_buffer"] = {"error": str(e)}

    # System resources
    try:
        result["system"] = _system_info()
    except Exception as e:
        result["system"] = {"error": str(e)}

    # Printers
    try:
        result["printers"] = _printer_stats()
    except Exception as e:
        result["printers"] = {"error": str(e)}

    # Modules
    try:
        result["modules"] = _module_ids()
    except Exception as e:
        result["modules"] = {"error": str(e)}

    # Supervisor
    try:
        result["supervisor"] = _supervisor_status()
    except Exception as e:
        result["supervisor"] = {"error": str(e)}

    # License tier (name only)
    try:
        result["license_tier"] = _license_tier()
    except Exception as e:
        result["license_tier"] = {"error": str(e)}

    # Database
    try:
        result["database"] = _database_info()
    except Exception as e:
        result["database"] = {"error": str(e)}

    return result
