"""O.D.I.N. — System, Config, Setup, Maintenance & Infrastructure Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response, UploadFile, File
from pydantic import BaseModel as PydanticBaseModel, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import text
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timedelta
import json
import logging
import os
import re
import shutil

import httpx

from deps import (
    get_db, get_current_user, require_role, log_audit, _validate_password, SessionLocal,
)
from models import (
    Printer, FilamentSlot, FilamentType, SystemConfig, MaintenanceTask, MaintenanceLog,
    Model, Job, Spool, FilamentLibrary,
)
from schemas import HealthCheck
from config import settings
from auth import hash_password, create_access_token
from license_manager import get_license, save_license_file
from branding import Branding, get_or_create_branding, branding_to_dict, UPDATABLE_FIELDS
import crypto

log = logging.getLogger("odin.api")
router = APIRouter()


# Read version from VERSION file
import pathlib as _pathlib
_version_file = _pathlib.Path(__file__).parent.parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    __version__ = "1.3.25"


# ============== Health Check ==============

@router.get("/health", response_model=HealthCheck, tags=["System"])
async def health_check():
    """Check API health and connectivity."""
    spoolman_ok = False
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
                spoolman_ok = resp.status_code == 200
        except:
            pass

    return HealthCheck(
        status="ok",
        version=__version__,
        database=settings.database_url.split("///")[-1],
        spoolman_connected=spoolman_ok
    )


# ============== License ==============

@router.get("/api/license", tags=["License"])
def get_license_info():
    """Get current license status. No auth required so frontend can check tier."""
    license_info = get_license()
    return license_info.to_dict()


@router.post("/api/license/upload", tags=["License"])
async def upload_license(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin")),
):
    """Upload a license file. Admin only."""
    content = await file.read()
    license_text = content.decode("utf-8").strip()

    # Handle both formats:
    # 1. JSON format from generate_license.py: {"format":"odin-license-v1","payload":"...","signature":"..."}
    # 2. Dot-separated format: base64_payload.base64_signature
    import json as _json
    try:
        license_json = _json.loads(license_text)
        if "payload" in license_json and "signature" in license_json:
            # Convert JSON format to dot-separated for storage
            license_text = license_json["payload"] + "." + license_json["signature"]
    except (ValueError, KeyError):
        pass  # Not JSON, try dot-separated format

    parts = license_text.split(".")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid license file format")

    # Save the file
    path = save_license_file(license_text)

    # Reload and validate
    license_info = get_license()
    if license_info.error:
        # Remove invalid file
        import os
        os.remove(path)
        raise HTTPException(status_code=400, detail=license_info.error)

    return {
        "status": "activated",
        "tier": license_info.tier,
        "licensee": license_info.licensee,
        "expires_at": license_info.expires_at,
    }


@router.delete("/api/license", tags=["License"])
def remove_license(
    current_user: dict = Depends(require_role("admin")),
):
    """Remove the license file (revert to Community tier). Admin only."""
    import os
    from license_manager import LICENSE_DIR, LICENSE_FILENAME, _cached_license
    license_path = os.path.join(LICENSE_DIR, LICENSE_FILENAME)
    if os.path.exists(license_path):
        os.remove(license_path)
    # Clear cache
    import license_manager
    license_manager._cached_license = None
    license_manager._cached_mtime = 0
    return {"status": "removed", "tier": "community"}


# ============== Setup / Onboarding Wizard ==============

class SetupAdminRequest(PydanticBaseModel):
    username: str
    email: str
    password: str
    role: str = "admin"

class SetupPrinterRequest(PydanticBaseModel):
    name: str
    model: Optional[str] = None
    api_type: Optional[str] = None
    api_host: Optional[str] = None
    api_key: Optional[str] = None
    slot_count: int = 4
    is_active: bool = True

class SetupTestPrinterRequest(PydanticBaseModel):
    api_type: str
    api_host: str
    serial: Optional[str] = None
    access_code: Optional[str] = None


def _setup_users_exist(db: Session) -> bool:
    """Check if any users exist in the database."""
    result = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    return result > 0


def _setup_is_complete(db: Session) -> bool:
    """Check if setup has been marked as complete."""
    row = db.execute(text(
        "SELECT value FROM system_config WHERE key = 'setup_complete'"
    )).fetchone()
    if row:
        return row[0] == "true"
    return False


def _setup_is_locked(db: Session) -> bool:
    """Setup is locked once users exist OR setup is explicitly marked complete."""
    return _setup_users_exist(db) or _setup_is_complete(db)


@router.get("/api/setup/status", tags=["Setup"])
def setup_status(db: Session = Depends(get_db)):
    """Check if initial setup is needed. No auth required."""
    has_users = _setup_users_exist(db)
    is_complete = _setup_is_complete(db)
    return {
        "needs_setup": not has_users and not is_complete,
        "has_users": has_users,
        "is_complete": is_complete,
    }


@router.post("/api/setup/admin", tags=["Setup"])
def setup_create_admin(request: SetupAdminRequest, db: Session = Depends(get_db)):
    """Create the first admin user during setup. Refuses if any user exists."""
    if _setup_users_exist(db):
        raise HTTPException(status_code=403, detail="Setup already completed — users exist")

    pw_valid, pw_msg = _validate_password(request.password)
    if not pw_valid:
        raise HTTPException(status_code=400, detail=pw_msg)
    password_hash_val = hash_password(request.password)
    try:
        db.execute(text("""
            INSERT INTO users (username, email, password_hash, role)
            VALUES (:username, :email, :password_hash, :role)
        """), {
            "username": request.username,
            "email": request.email,
            "password_hash": password_hash_val,
            "role": "admin"
        })
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create user: {str(e)}")

    # Return a JWT token so the wizard can make authenticated calls
    access_token = create_access_token(
        data={"sub": request.username, "role": "admin"}
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/api/setup/test-printer", tags=["Setup"])
def setup_test_printer(request: SetupTestPrinterRequest, db: Session = Depends(get_db)):
    """Test printer connection during setup. Wraps existing test logic."""
    if _setup_is_locked(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    if request.api_type.lower() == "bambu":
        if not request.serial or not request.access_code:
            raise HTTPException(status_code=400, detail="Serial and access_code required for Bambu printers")
        try:
            from bambu_adapter import BambuPrinter
            import time

            bambu = BambuPrinter(
                ip=request.api_host,
                serial=request.serial,
                access_code=request.access_code
            )
            if not bambu.connect():
                return {"success": False, "error": "Failed to connect. Check IP, serial, and access code."}

            time.sleep(2)
            bambu_status = bambu.get_status()
            bambu.disconnect()

            return {
                "success": True,
                "state": bambu_status.state.value,
                "bed_temp": bambu_status.bed_temp,
                "nozzle_temp": bambu_status.nozzle_temp,
                "ams_slots": len(bambu_status.ams_slots)
            }
        except ImportError:
            raise HTTPException(status_code=500, detail="bambu_adapter not installed")
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif request.api_type.lower() == "moonraker":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}/printer/info", timeout=5)
            if r.status_code == 200:
                info = r.json().get("result", {})
                return {
                    "success": True,
                    "state": info.get("state", "unknown"),
                    "bed_temp": 0,
                    "nozzle_temp": 0,
                    "ams_slots": 0,
                }
            return {"success": False, "error": f"Moonraker returned {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unknown printer type: {request.api_type}"}


@router.post("/api/setup/printer", tags=["Setup"])
def setup_create_printer(request: SetupPrinterRequest, db: Session = Depends(get_db)):
    """Create a printer during setup. Requires JWT from admin creation step."""
    if _setup_is_locked(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    # Encrypt api_key if provided
    encrypted_api_key = None
    if request.api_key:
        encrypted_api_key = crypto.encrypt(request.api_key)

    db_printer = Printer(
        name=request.name,
        model=request.model,
        slot_count=request.slot_count,
        is_active=request.is_active,
        api_type=request.api_type,
        api_host=request.api_host,
        api_key=encrypted_api_key,
    )
    db.add(db_printer)
    db.flush()

    # Create empty filament slots
    for i in range(1, request.slot_count + 1):
        slot = FilamentSlot(
            printer_id=db_printer.id,
            slot_number=i,
            filament_type=FilamentType.EMPTY,
        )
        db.add(slot)

    db.commit()
    db.refresh(db_printer)
    return {"id": db_printer.id, "name": db_printer.name, "status": "created"}


@router.post("/api/setup/complete", tags=["Setup"])
def setup_mark_complete(db: Session = Depends(get_db)):
    """Mark setup as complete. Prevents wizard from showing again."""
    if _setup_is_locked(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    existing = db.execute(text(
        "SELECT key FROM system_config WHERE key = 'setup_complete'"
    )).fetchone()

    if existing:
        db.execute(text(
            "UPDATE system_config SET value = 'true' WHERE key = 'setup_complete'"
        ))
    else:
        # Insert using the SystemConfig model pattern
        config = SystemConfig(key="setup_complete", value="true")
        db.add(config)

    db.commit()
    return {"status": "complete"}


def _get_lan_ip():
    """Auto-detect LAN IP for WebRTC ICE candidates."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


@router.get("/api/setup/network", tags=["Setup"])
def setup_network_info(request: Request):
    """Return auto-detected host IP for network configuration."""
    # Best detection: use the Host header from the browser request
    # When user hits http://192.168.70.200:8000, Host = "192.168.70.200:8000"
    detected_ip = ""
    host_header = request.headers.get("host", "")
    host_part = host_header.split(":")[0] if host_header else ""
    # Only use if it looks like a real LAN IP (not localhost, not Docker internal)
    import re as _re
    if host_part and _re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_part):
        if not host_part.startswith("127.") and not host_part.startswith("172."):
            detected_ip = host_part
    # Fallback to socket detection (works on bare metal, useless in Docker)
    if not detected_ip:
        detected_ip = _get_lan_ip() or ""
    return {"detected_ip": detected_ip, "configured_ip": os.environ.get("ODIN_HOST_IP", "")}


@router.post("/api/setup/network", tags=["Setup"])
async def setup_save_network(request: Request, db: Session = Depends(get_db)):
    """Save host IP for WebRTC camera streaming."""
    data = await request.json()
    host_ip = data.get("host_ip", "").strip()
    if not host_ip:
        raise HTTPException(status_code=400, detail="host_ip is required")
    import re
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_ip):
        raise HTTPException(status_code=400, detail="Invalid IP address format")
    existing = db.execute(text("SELECT 1 FROM system_config WHERE key = 'host_ip'")).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = :v WHERE key = 'host_ip'"), {"v": host_ip})
    else:
        db.execute(text("INSERT INTO system_config (key, value) VALUES ('host_ip', :v)"), {"v": host_ip})
    db.commit()
    from routers.printers import sync_go2rtc_config
    sync_go2rtc_config(db)
    return {"success": True, "host_ip": host_ip}


# ============== Config ==============

class ConfigUpdate(PydanticBaseModel):
    """Validated config update request."""
    spoolman_url: Optional[str] = None
    blackout_start: Optional[str] = None
    blackout_end: Optional[str] = None

    @field_validator('spoolman_url')
    @classmethod
    def validate_url(cls, v):
        if v is None or v == '':
            return v
        # Basic URL validation
        url_pattern = re.compile(
            r'^https?://'  # http:// or https://
            r'[a-zA-Z0-9]+'  # domain start
            r'[a-zA-Z0-9.-]*'  # domain rest
            r'(:\d+)?'  # optional port
            r'(/.*)?$'  # optional path
        )
        if not url_pattern.match(v):
            raise ValueError('Invalid URL format. Must be http:// or https://')
        return v

    @field_validator('blackout_start', 'blackout_end')
    @classmethod
    def validate_time(cls, v):
        if v is None:
            return v
        # Validate HH:MM format
        time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$')
        if not time_pattern.match(v):
            raise ValueError('Invalid time format. Use HH:MM (e.g., 22:30)')
        return v


# Allowed config keys that can be written to .env
ALLOWED_CONFIG_KEYS = {'SPOOLMAN_URL', 'BLACKOUT_START', 'BLACKOUT_END'}


@router.get("/api/config", tags=["Config"])
def get_config():
    """Get current configuration."""
    return {
        "spoolman_url": settings.spoolman_url,
        "blackout_start": settings.blackout_start,
        "blackout_end": settings.blackout_end,
    }

@router.put("/api/config", tags=["Config"])
def update_config(config: ConfigUpdate, current_user: dict = Depends(require_role("admin"))):
    """Update configuration. Writes to .env file."""
    # Use environment variable or default path
    env_path = os.environ.get('ENV_FILE_PATH', '/data/.env')

    # Read existing env, only keeping allowed keys
    env_vars = {}
    try:
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    key, val = line.strip().split('=', 1)
                    # Preserve existing keys (including ones we don't manage like API_KEY)
                    env_vars[key] = val
    except FileNotFoundError:
        pass

    # Update only the values that were provided and are allowed
    if config.spoolman_url is not None:
        env_vars['SPOOLMAN_URL'] = config.spoolman_url
    if config.blackout_start is not None:
        env_vars['BLACKOUT_START'] = config.blackout_start
    if config.blackout_end is not None:
        env_vars['BLACKOUT_END'] = config.blackout_end

    # Write back
    with open(env_path, 'w') as f:
        for key, val in env_vars.items():
            f.write(f"{key}={val}\n")

    return {"success": True, "message": "Config updated. Restart backend to apply changes."}

@router.get("/api/spoolman/test", tags=["Spoolman"])
async def test_spoolman_connection():
    """Test Spoolman connection."""
    if not settings.spoolman_url:
        return {"success": False, "message": "Spoolman URL not configured"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
            if resp.status_code == 200:
                return {"success": True, "message": f"Connected to Spoolman at {settings.spoolman_url}"}
            else:
                return {"success": False, "message": f"Spoolman returned status {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ============== IP Allowlist ==============

@router.get("/api/config/ip-allowlist", tags=["Config"])
async def get_ip_allowlist(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get the IP allowlist configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'ip_allowlist'")).fetchone()
    if not row:
        return {"enabled": False, "cidrs": [], "mode": "api_and_ui"}
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return val


@router.put("/api/config/ip-allowlist", tags=["Config"])
async def set_ip_allowlist(request: Request, body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set the IP allowlist. Includes lock-out protection."""
    import ipaddress
    enabled = body.get("enabled", False)
    cidrs = body.get("cidrs", [])
    mode = body.get("mode", "api_and_ui")

    # Validate CIDRs
    for cidr in cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid CIDR: {cidr}")

    # Lock-out protection: always include the requester's IP
    client_ip = request.client.host if request.client else "127.0.0.1"
    if enabled and cidrs:
        client_in_list = any(
            ipaddress.ip_address(client_ip) in ipaddress.ip_network(c, strict=False) for c in cidrs
        )
        if not client_in_list:
            cidrs.append(client_ip + "/32")

    config = {"enabled": enabled, "cidrs": cidrs, "mode": mode}
    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('ip_allowlist', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})
    db.commit()

    log_audit(db, "ip_allowlist_updated", details=f"Enabled={enabled}, {len(cidrs)} CIDRs")
    return config


# ============== Retention Config ==============

RETENTION_DEFAULTS = {
    "completed_jobs_days": 0,       # 0 = unlimited
    "audit_logs_days": 365,
    "timelapses_days": 30,
    "alert_history_days": 90,
}


@router.get("/api/config/retention", tags=["Config"])
async def get_retention_config(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Get data retention policy configuration."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    if not row:
        return RETENTION_DEFAULTS
    val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
    return {**RETENTION_DEFAULTS, **val}


@router.put("/api/config/retention", tags=["Config"])
async def set_retention_config(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set data retention policy configuration."""
    config = {}
    for key in RETENTION_DEFAULTS:
        if key in body:
            val = int(body[key])
            if val < 0:
                raise HTTPException(status_code=400, detail=f"{key} must be >= 0")
            config[key] = val

    db.execute(text("""INSERT INTO system_config (key, value) VALUES ('data_retention', :val)
                       ON CONFLICT(key) DO UPDATE SET value = :val"""),
               {"val": json.dumps(config)})
    db.commit()

    log_audit(db, "retention_updated", details=f"Retention config: {config}")
    return {**RETENTION_DEFAULTS, **config}


@router.post("/api/admin/retention/cleanup", tags=["Config"])
async def run_retention_cleanup(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Manually trigger data retention cleanup."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'data_retention'")).fetchone()
    config = {**RETENTION_DEFAULTS}
    if row:
        val = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
        config.update(val)

    deleted = {}
    now = datetime.now()

    if config["completed_jobs_days"] > 0:
        cutoff = now - timedelta(days=config["completed_jobs_days"])
        r = db.execute(text("DELETE FROM jobs WHERE status IN ('completed','failed','cancelled') AND updated_at < :cutoff"),
                       {"cutoff": cutoff})
        deleted["completed_jobs"] = r.rowcount

    if config["audit_logs_days"] > 0:
        cutoff = now - timedelta(days=config["audit_logs_days"])
        r = db.execute(text("DELETE FROM audit_log WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["audit_logs"] = r.rowcount

    if config["alert_history_days"] > 0:
        cutoff = now - timedelta(days=config["alert_history_days"])
        r = db.execute(text("DELETE FROM alerts WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["alerts"] = r.rowcount

    if config["timelapses_days"] > 0:
        cutoff = now - timedelta(days=config["timelapses_days"])
        r = db.execute(text("DELETE FROM timelapses WHERE created_at < :cutoff"), {"cutoff": cutoff})
        deleted["timelapses"] = r.rowcount

    # Clean expired token blacklist entries
    db.execute(text("DELETE FROM token_blacklist WHERE expires_at < :now"), {"now": now})
    # Clean stale sessions (older than 24h with no JWT to match)
    stale = now - timedelta(hours=48)
    db.execute(text("DELETE FROM active_sessions WHERE last_seen_at < :cutoff"), {"cutoff": stale})

    db.commit()
    return {"status": "ok", "deleted": deleted}


# ============== Backup Restore ==============

@router.post("/api/backups/restore", tags=["System"])
async def restore_backup(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Restore database from an uploaded backup file."""
    import sqlite3
    import tempfile

    if not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db files are supported")

    # Save uploaded file to temp
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    # Validate the uploaded DB
    try:
        test_conn = sqlite3.connect(tmp_path)
        result = test_conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail="Backup file failed integrity check")
        # Check it has a users table
        tables = [r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "users" not in tables:
            test_conn.close()
            os.unlink(tmp_path)
            raise HTTPException(status_code=400, detail="Backup file is not a valid O.D.I.N. database")
        test_conn.close()
    except sqlite3.Error as e:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=f"Invalid database file: {e}")

    # Auto-backup current DB before restore
    db_path = "/data/odin.db"
    backup_dir = "/data/backups"
    os.makedirs(backup_dir, exist_ok=True)
    pre_restore_name = f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(db_path, os.path.join(backup_dir, pre_restore_name))

    # Replace the DB file
    shutil.copy2(tmp_path, db_path)
    os.unlink(tmp_path)

    log_audit(db, "backup_restored", details=f"Restored from {file.filename}, pre-restore backup: {pre_restore_name}")

    return {
        "status": "ok",
        "message": "Database restored. Restart the container to apply changes.",
        "pre_restore_backup": pre_restore_name,
    }


# ============== Prometheus Metrics ==============

@router.get("/metrics", tags=["Monitoring"])
async def prometheus_metrics(db: Session = Depends(get_db)):
    """Prometheus-compatible metrics endpoint. No auth required."""
    lines = []

    # --- Fleet metrics ---
    printers_all = db.execute(text("SELECT * FROM printers WHERE is_active = 1")).fetchall()
    total_printers = len(printers_all)
    online_count = 0
    printing_count = 0
    idle_count = 0
    error_count = 0

    from datetime import datetime, timedelta
    now = datetime.utcnow()

    for p in printers_all:
        pm = dict(p._mapping)
        last_seen = pm.get("last_seen")
        is_online = False
        if last_seen:
            try:
                ls = datetime.fromisoformat(str(last_seen).replace("Z", ""))
                is_online = (now - ls).total_seconds() < 90
            except Exception:
                pass

        if is_online:
            online_count += 1
            gcode_state = pm.get("gcode_state", "")
            if gcode_state in ("RUNNING", "PREPARE"):
                printing_count += 1
            elif gcode_state in ("FAILED", "UNKNOWN"):
                error_count += 1
            else:
                idle_count += 1

    lines.append("# HELP odin_printers_total Total registered printers")
    lines.append("# TYPE odin_printers_total gauge")
    lines.append(f"odin_printers_total {total_printers}")

    lines.append("# HELP odin_printers_online Online printers (seen in last 90s)")
    lines.append("# TYPE odin_printers_online gauge")
    lines.append(f"odin_printers_online {online_count}")

    lines.append("# HELP odin_printers_printing Currently printing")
    lines.append("# TYPE odin_printers_printing gauge")
    lines.append(f"odin_printers_printing {printing_count}")

    lines.append("# HELP odin_printers_idle Online but idle")
    lines.append("# TYPE odin_printers_idle gauge")
    lines.append(f"odin_printers_idle {idle_count}")

    lines.append("# HELP odin_printers_error Online with errors")
    lines.append("# TYPE odin_printers_error gauge")
    lines.append(f"odin_printers_error {error_count}")

    # --- Per-printer telemetry ---
    lines.append("# HELP odin_printer_nozzle_temp_celsius Current nozzle temperature")
    lines.append("# TYPE odin_printer_nozzle_temp_celsius gauge")
    lines.append("# HELP odin_printer_bed_temp_celsius Current bed temperature")
    lines.append("# TYPE odin_printer_bed_temp_celsius gauge")
    lines.append("# HELP odin_printer_progress Print progress 0-100")
    lines.append("# TYPE odin_printer_progress gauge")
    lines.append("# HELP odin_printer_print_hours_total Lifetime print hours")
    lines.append("# TYPE odin_printer_print_hours_total counter")
    lines.append("# HELP odin_printer_print_count_total Lifetime print count")
    lines.append("# TYPE odin_printer_print_count_total counter")

    for p in printers_all:
        pm = dict(p._mapping)
        name = pm.get("nickname") or pm.get("name", f"printer_{pm['id']}")
        pid = pm["id"]
        labels = f'printer="{name}",printer_id="{pid}"'

        nozzle = pm.get("nozzle_temp")
        bed = pm.get("bed_temp")
        progress = pm.get("print_progress")
        total_hours = pm.get("total_print_hours", 0) or 0
        total_prints = pm.get("total_print_count", 0) or 0

        if nozzle is not None:
            lines.append(f"odin_printer_nozzle_temp_celsius{{{labels}}} {nozzle}")
        if bed is not None:
            lines.append(f"odin_printer_bed_temp_celsius{{{labels}}} {bed}")
        if progress is not None:
            lines.append(f"odin_printer_progress{{{labels}}} {progress}")
        lines.append(f"odin_printer_print_hours_total{{{labels}}} {total_hours}")
        lines.append(f"odin_printer_print_count_total{{{labels}}} {total_prints}")

    # --- Job metrics ---
    job_counts = db.execute(text("""
        SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status
    """)).fetchall()

    lines.append("# HELP odin_jobs_by_status Number of jobs by status")
    lines.append("# TYPE odin_jobs_by_status gauge")
    for row in job_counts:
        r = dict(row._mapping)
        lines.append(f'odin_jobs_by_status{{status="{r["status"]}"}} {r["cnt"]}')

    # Queue depth (pending + scheduled)
    queue = db.execute(text("""
        SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('pending', 'scheduled', 'submitted')
    """)).fetchone()
    lines.append("# HELP odin_queue_depth Jobs waiting to print")
    lines.append("# TYPE odin_queue_depth gauge")
    lines.append(f"odin_queue_depth {dict(queue._mapping)['cnt']}")

    # --- Spool metrics ---
    spool_data = db.execute(text("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN remaining_weight_g < 100 THEN 1 ELSE 0 END) as low
        FROM spools WHERE remaining_weight_g IS NOT NULL
    """)).fetchone()
    sd = dict(spool_data._mapping)

    lines.append("# HELP odin_spools_total Total tracked spools")
    lines.append("# TYPE odin_spools_total gauge")
    lines.append(f"odin_spools_total {sd['total'] or 0}")

    lines.append("# HELP odin_spools_low Spools under 100g remaining")
    lines.append("# TYPE odin_spools_low gauge")
    lines.append(f"odin_spools_low {sd['low'] or 0}")

    # --- Order metrics ---
    order_data = db.execute(text("""
        SELECT status, COUNT(*) as cnt FROM orders GROUP BY status
    """)).fetchall()

    lines.append("# HELP odin_orders_by_status Orders by status")
    lines.append("# TYPE odin_orders_by_status gauge")
    for row in order_data:
        r = dict(row._mapping)
        lines.append(f'odin_orders_by_status{{status="{r["status"]}"}} {r["cnt"]}')

    # --- Alert metrics ---
    unread = db.execute(text("SELECT COUNT(*) as cnt FROM alerts WHERE is_read = 0")).fetchone()
    lines.append("# HELP odin_alerts_unread Unread alerts")
    lines.append("# TYPE odin_alerts_unread gauge")
    lines.append(f"odin_alerts_unread {dict(unread._mapping)['cnt']}")

    from starlette.responses import Response
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8"
    )


# ============== HMS Code Lookup ==============

@router.get("/api/hms-codes/{code}", tags=["Monitoring"])
async def lookup_hms(code: str):
    """Look up human-readable description for a Bambu HMS error code."""
    try:
        from hms_codes import lookup_hms_code, get_code_count
        return {
            "code": code,
            "message": lookup_hms_code(code),
            "total_codes": get_code_count()
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ============== Quiet Hours Config ==============

@router.get("/api/config/quiet-hours")
async def get_quiet_hours_config(db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Get quiet hours settings."""
    keys = ["quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end", "quiet_hours_digest"]
    config = {}
    defaults = {"enabled": False, "start": "22:00", "end": "07:00", "digest": True}

    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("quiet_hours_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "digest"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            else:
                config[short_key] = val
        else:
            config[short_key] = defaults.get(short_key, "")
    return config


@router.put("/api/config/quiet-hours")
async def update_quiet_hours_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update quiet hours settings. Admin only."""

    body = await request.json()

    for short_key, value in body.items():
        db_key = f"quiet_hours_{short_key}"
        str_val = str(value).lower() if isinstance(value, bool) else str(value)

        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"),
                       {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"),
                       {"k": db_key, "v": str_val})

    db.commit()

    # Invalidate cache
    try:
        from quiet_hours import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    return {"status": "ok"}


# ============== MQTT Republish Configuration ==============
try:
    import mqtt_republish
except ImportError:
    mqtt_republish = None

@router.get("/api/config/mqtt-republish")
async def get_mqtt_republish_config(db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Get MQTT republish settings."""
    keys = [
        "mqtt_republish_enabled", "mqtt_republish_host", "mqtt_republish_port",
        "mqtt_republish_username", "mqtt_republish_password",
        "mqtt_republish_topic_prefix", "mqtt_republish_use_tls",
    ]
    config = {}
    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("mqtt_republish_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "use_tls"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            elif short_key == "port":
                config[short_key] = int(val) if val else 1883
            elif short_key == "password":
                config[short_key] = "••••••••" if val else ""
            else:
                config[short_key] = val
        else:
            defaults = {"enabled": False, "host": "", "port": 1883, "username": "",
                        "password": "", "topic_prefix": "odin", "use_tls": False}
            config[short_key] = defaults.get(short_key, "")
    return config


@router.put("/api/config/mqtt-republish")
async def update_mqtt_republish_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update MQTT republish settings. Admin only."""

    body = await request.json()

    for short_key, value in body.items():
        db_key = f"mqtt_republish_{short_key}"
        # Don't overwrite password if it's the masked value
        if short_key == "password" and value == "••••••••":
            continue

        str_val = str(value).lower() if isinstance(value, bool) else str(value)

        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"),
                       {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"),
                       {"k": db_key, "v": str_val})

    db.commit()

    # Invalidate the republish module's cached config
    if mqtt_republish:
        mqtt_republish.invalidate_cache()

    return {"status": "ok"}


@router.post("/api/config/mqtt-republish/test")
async def test_mqtt_republish(request: Request, current_user: dict = Depends(require_role("admin"))):
    """Test connection to external MQTT broker."""
    # Role check handled by require_role("admin") dependency

    if not mqtt_republish:
        raise HTTPException(status_code=503, detail="MQTT republish module not available")

    body = await request.json()
    result = mqtt_republish.test_connection(
        host=body.get("host", ""),
        port=int(body.get("port", 1883)),
        username=body.get("username", ""),
        password=body.get("password", ""),
        use_tls=body.get("use_tls", False),
        topic_prefix=body.get("topic_prefix", "odin"),
    )
    return result


# ============== Branding ==============

@router.get("/api/branding", tags=["Branding"])
async def get_branding(db: Session = Depends(get_db)):
    """Get branding config. PUBLIC - no auth required."""
    return branding_to_dict(get_or_create_branding(db))


@router.put("/api/branding", tags=["Branding"])
async def update_branding(data: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update branding config. Admin only."""
    branding = get_or_create_branding(db)
    for key, value in data.items():
        if key in UPDATABLE_FIELDS:
            setattr(branding, key, value)
    branding.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(branding)
    return branding_to_dict(branding)


@router.post("/api/branding/logo", tags=["Branding"])
async def upload_logo(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Upload brand logo. Admin only."""
    allowed = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "..", "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"logo.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.logo_url = f"/static/branding/{filename}"
    db.commit()
    return {"logo_url": branding.logo_url}


@router.post("/api/branding/favicon", tags=["Branding"])
async def upload_favicon(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Upload favicon. Admin only."""
    allowed = {"image/png", "image/x-icon", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "..", "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"favicon.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.favicon_url = f"/static/branding/{filename}"
    db.commit()
    return {"favicon_url": branding.favicon_url}


@router.delete("/api/branding/logo", tags=["Branding"])
async def remove_logo(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Remove brand logo. Admin only."""
    branding = get_or_create_branding(db)
    if branding.logo_url:
        filepath = os.path.join(os.path.dirname(__file__), "..", branding.logo_url.lstrip("/"))
        if os.path.exists(filepath):
            os.remove(filepath)
    branding.logo_url = None
    db.commit()
    return {"logo_url": None}


# ============== Database Backups ==============

@router.post("/api/backups", tags=["System"])
def create_backup(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Create a database backup using SQLite online backup API."""
    import sqlite3 as sqlite3_mod

    backup_dir = Path(__file__).parent.parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    # Resolve DB file path from engine URL
    engine_url = str(db.get_bind().url)
    if "///" in engine_url:
        db_path = engine_url.split("///", 1)[1]
    else:
        db_path = "odin.db"
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", db_path)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"odin_backup_{timestamp}.db"
    backup_path = str(backup_dir / backup_name)

    # Use SQLite online backup API — safe while DB is in use
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
        "created_at": datetime.utcnow().isoformat()
    }


@router.get("/api/backups", tags=["System"])
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


@router.get("/api/backups/{filename}", tags=["System"])
def download_backup(filename: str, current_user: dict = Depends(require_role("admin"))):
    """Download a database backup file."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    backup_dir = Path(__file__).parent.parent / "backups"
    backup_path = backup_dir / filename

    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    from starlette.responses import FileResponse
    return FileResponse(
        path=str(backup_path),
        filename=filename,
        media_type="application/octet-stream"
    )


@router.delete("/api/backups/{filename}", status_code=status.HTTP_204_NO_CONTENT, tags=["System"])
def delete_backup(filename: str, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a database backup."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    backup_dir = Path(__file__).parent.parent / "backups"
    backup_path = backup_dir / filename

    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")

    backup_path.unlink()
    log_audit(db, "backup_deleted", "system", details={"filename": filename})


# ============== Language / i18n ==============

@router.get("/api/settings/language", tags=["Settings"])
async def get_language(db: Session = Depends(get_db)):
    """Get current interface language."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'language'")).fetchone()
    return {"language": result[0] if result else "en"}


@router.put("/api/settings/language", tags=["Settings"])
async def set_language(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set interface language."""
    data = await request.json()
    lang = data.get("language", "en")
    supported = ["en", "de", "ja", "es"]
    if lang not in supported:
        raise HTTPException(400, f"Unsupported language. Choose from: {', '.join(supported)}")
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('language', :lang)"
    ), {"lang": lang})
    db.commit()
    return {"language": lang}


# ============== Maintenance Tracking ==============

class MaintenanceTaskCreate(PydanticBaseModel):
    name: str
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: float = 0
    estimated_downtime_min: int = 30


class MaintenanceTaskUpdate(PydanticBaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: Optional[float] = None
    estimated_downtime_min: Optional[int] = None
    is_active: Optional[bool] = None


class MaintenanceLogCreate(PydanticBaseModel):
    printer_id: int
    task_id: Optional[int] = None
    task_name: str
    performed_by: Optional[str] = None
    notes: Optional[str] = None
    cost: float = 0
    downtime_minutes: int = 0


@router.get("/api/maintenance/tasks", tags=["Maintenance"])
def list_maintenance_tasks(db: Session = Depends(get_db)):
    """List all maintenance task templates."""
    tasks = db.query(MaintenanceTask).order_by(MaintenanceTask.name).all()
    return [{
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "printer_model_filter": t.printer_model_filter,
        "interval_print_hours": t.interval_print_hours,
        "interval_days": t.interval_days,
        "estimated_cost": t.estimated_cost,
        "estimated_downtime_min": t.estimated_downtime_min,
        "is_active": t.is_active,
    } for t in tasks]


@router.post("/api/maintenance/tasks", tags=["Maintenance"])
def create_maintenance_task(data: MaintenanceTaskCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new maintenance task template."""
    task = MaintenanceTask(
        name=data.name,
        description=data.description,
        printer_model_filter=data.printer_model_filter,
        interval_print_hours=data.interval_print_hours,
        interval_days=data.interval_days,
        estimated_cost=data.estimated_cost,
        estimated_downtime_min=data.estimated_downtime_min,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id, "name": task.name, "message": "Task created"}


@router.patch("/api/maintenance/tasks/{task_id}", tags=["Maintenance"])
def update_maintenance_task(task_id: int, data: MaintenanceTaskUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a maintenance task template."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return {"id": task.id, "message": "Task updated"}


@router.delete("/api/maintenance/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_task(task_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a maintenance task template and its logs."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()


@router.get("/api/maintenance/logs", tags=["Maintenance"])
def list_maintenance_logs(
    printer_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """List maintenance logs, optionally filtered by printer."""
    query = db.query(MaintenanceLog).order_by(MaintenanceLog.performed_at.desc())
    if printer_id:
        query = query.filter(MaintenanceLog.printer_id == printer_id)
    logs = query.limit(limit).all()
    return [{
        "id": l.id,
        "printer_id": l.printer_id,
        "task_id": l.task_id,
        "task_name": l.task_name,
        "performed_at": l.performed_at.isoformat() if l.performed_at else None,
        "performed_by": l.performed_by,
        "notes": l.notes,
        "cost": l.cost,
        "downtime_minutes": l.downtime_minutes,
        "print_hours_at_service": l.print_hours_at_service,
    } for l in logs]


@router.post("/api/maintenance/logs", tags=["Maintenance"])
def create_maintenance_log(data: MaintenanceLogCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Log a maintenance action performed on a printer."""
    printer = db.query(Printer).filter(Printer.id == data.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Compute current total print hours for this printer (from completed jobs)
    result = db.execute(text(
        "SELECT COALESCE(SUM(duration_hours), 0) FROM jobs "
        "WHERE printer_id = :pid AND status = 'COMPLETED'"
    ), {"pid": data.printer_id}).scalar()
    total_hours = float(result or 0)

    log_entry = MaintenanceLog(
        printer_id=data.printer_id,
        task_id=data.task_id,
        task_name=data.task_name,
        performed_by=data.performed_by,
        notes=data.notes,
        cost=data.cost,
        downtime_minutes=data.downtime_minutes,
        print_hours_at_service=total_hours,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)
    return {"id": log_entry.id, "message": "Maintenance logged", "print_hours_at_service": total_hours}


@router.delete("/api/maintenance/logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_log(log_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a maintenance log entry."""
    log_entry = db.query(MaintenanceLog).filter(MaintenanceLog.id == log_id).first()
    if not log_entry:
        raise HTTPException(status_code=404, detail="Log not found")
    db.delete(log_entry)
    db.commit()


@router.get("/api/maintenance/status", tags=["Maintenance"])
def get_maintenance_status(db: Session = Depends(get_db)):
    """Get maintenance status for all active printers. Returns per-printer task health."""
    printers = db.query(Printer).filter(Printer.is_active == True).order_by(Printer.name).all()
    tasks = db.query(MaintenanceTask).filter(MaintenanceTask.is_active == True).all()

    # Total print hours per printer (from care counters, updated by monitors)
    # Note: Previously calculated from jobs table, now using real-time counter
    hours_map = {p.id: float(p.total_print_hours or 0) for p in printers}

    # Latest maintenance log per (printer_id, task_id)
    all_logs = db.query(MaintenanceLog).all()
    log_map = {}
    for mlog in all_logs:
        key = (mlog.printer_id, mlog.task_id)
        if key not in log_map or (mlog.performed_at and log_map[key].performed_at and mlog.performed_at > log_map[key].performed_at):
            log_map[key] = mlog

    now = datetime.utcnow()
    result = []

    for printer in printers:
        total_hours = hours_map.get(printer.id, 0)
        printer_tasks = []
        worst_status = "ok"

        for task in tasks:
            # Filter: does this task apply to this printer?
            if task.printer_model_filter:
                if task.printer_model_filter.lower() not in (printer.model or "").lower():
                    continue

            last_log = log_map.get((printer.id, task.id))

            if last_log and last_log.performed_at:
                hours_since = total_hours - (last_log.print_hours_at_service or 0)
                days_since = (now - last_log.performed_at).days
                last_serviced = last_log.performed_at.isoformat()
                last_by = last_log.performed_by
            else:
                hours_since = total_hours
                days_since = (now - printer.created_at).days if printer.created_at else 0
                last_serviced = None
                last_by = None

            # Determine status
            task_status = "ok"
            progress = 0.0

            if task.interval_print_hours and task.interval_print_hours > 0:
                pct = (hours_since / task.interval_print_hours) * 100
                progress = max(progress, pct)
                if hours_since >= task.interval_print_hours:
                    task_status = "overdue"
                elif hours_since >= task.interval_print_hours * 0.8:
                    task_status = "due_soon"

            if task.interval_days and task.interval_days > 0:
                pct = (days_since / task.interval_days) * 100
                progress = max(progress, pct)
                if days_since >= task.interval_days:
                    task_status = "overdue"
                elif days_since >= task.interval_days * 0.8:
                    if task_status != "overdue":
                        task_status = "due_soon"

            if task_status == "overdue":
                worst_status = "overdue"
            elif task_status == "due_soon" and worst_status == "ok":
                worst_status = "due_soon"

            printer_tasks.append({
                "task_id": task.id,
                "task_name": task.name,
                "description": task.description,
                "interval_print_hours": task.interval_print_hours,
                "interval_days": task.interval_days,
                "hours_since_service": round(hours_since, 1),
                "days_since_service": days_since,
                "last_serviced": last_serviced,
                "last_by": last_by,
                "status": task_status,
                "progress_percent": round(min(progress, 150), 1),
            })

        result.append({
            "printer_id": printer.id,
            "printer_name": printer.name,
            "printer_model": printer.model,
            "total_print_hours": round(total_hours, 1),
            "tasks": sorted(printer_tasks, key=lambda t: {"overdue": 0, "due_soon": 1, "ok": 2}.get(t["status"], 3)),
            "overall_status": worst_status,
        })

    # Sort: overdue printers first
    result.sort(key=lambda p: {"overdue": 0, "due_soon": 1, "ok": 2}.get(p["overall_status"], 3))
    return result


@router.post("/api/maintenance/seed-defaults", tags=["Maintenance"])
def seed_default_maintenance_tasks(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Seed default maintenance tasks for common Bambu Lab printer models."""
    defaults = [
        # Universal tasks (all printers)
        {"name": "General Cleaning", "description": "Clean build plate, wipe exterior, clear debris from print area",
         "printer_model_filter": None, "interval_print_hours": 50, "interval_days": 14,
         "estimated_cost": 0, "estimated_downtime_min": 15},
        {"name": "Nozzle Inspection", "description": "Check nozzle for wear, clogs, or damage — replace if needed",
         "printer_model_filter": None, "interval_print_hours": 500, "interval_days": None,
         "estimated_cost": 8, "estimated_downtime_min": 15},
        {"name": "Build Plate Check", "description": "Inspect build plate surface — clean, re-level, or replace if worn",
         "printer_model_filter": None, "interval_print_hours": 1000, "interval_days": 180,
         "estimated_cost": 30, "estimated_downtime_min": 10},
        {"name": "Belt Tension Check", "description": "Verify X/Y belt tension and adjust if loose",
         "printer_model_filter": None, "interval_print_hours": 500, "interval_days": None,
         "estimated_cost": 0, "estimated_downtime_min": 20},
        {"name": "Firmware Update Check", "description": "Check for and apply firmware updates",
         "printer_model_filter": None, "interval_print_hours": None, "interval_days": 30,
         "estimated_cost": 0, "estimated_downtime_min": 15},
        # X1C / X1E specific
        {"name": "Carbon Rod Lubrication", "description": "Lubricate carbon rods on X/Y axes (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 5, "estimated_downtime_min": 20},
        {"name": "HEPA Filter Replacement", "description": "Replace HEPA filter in enclosure (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 500, "interval_days": 90,
         "estimated_cost": 12, "estimated_downtime_min": 5},
        {"name": "Purge Wiper Replacement", "description": "Replace purge/wiper assembly (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 6, "estimated_downtime_min": 10},
        # P1S specific
        {"name": "HEPA Filter Replacement", "description": "Replace HEPA filter in enclosure (P1S)",
         "printer_model_filter": "P1S", "interval_print_hours": 500, "interval_days": 90,
         "estimated_cost": 12, "estimated_downtime_min": 5},
        {"name": "Carbon Rod Lubrication", "description": "Lubricate carbon rods on X/Y axes (P1S)",
         "printer_model_filter": "P1S", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 5, "estimated_downtime_min": 20},
        # A1 specific
        {"name": "Hotend Cleaning", "description": "Clean hotend assembly and check for leaks (A1 series)",
         "printer_model_filter": "A1", "interval_print_hours": 300, "interval_days": None,
         "estimated_cost": 0, "estimated_downtime_min": 20},
    ]

    created = 0
    skipped = 0
    for d in defaults:
        existing = db.query(MaintenanceTask).filter(
            MaintenanceTask.name == d["name"],
            MaintenanceTask.printer_model_filter == d["printer_model_filter"]
        ).first()
        if not existing:
            task = MaintenanceTask(**d)
            db.add(task)
            created += 1
        else:
            skipped += 1

    db.commit()
    return {"message": f"Seeded {created} tasks ({skipped} already existed)", "created": created, "skipped": skipped}


# ============== Global Search ==============

@router.get("/api/search", tags=["Search"])
def global_search(q: str = "", db: Session = Depends(get_db)):
    """Search across models, jobs, spools, and printers."""
    if not q or len(q) < 2:
        return {"models": [], "jobs": [], "spools": [], "printers": []}

    query = f"%{q.lower()}%"

    # Search models
    models = db.query(Model).filter(
        (Model.name.ilike(query)) | (Model.notes.ilike(query))
    ).limit(5).all()

    # Search jobs
    jobs = db.query(Job).filter(
        (Job.item_name.ilike(query)) | (Job.notes.ilike(query))
    ).order_by(Job.created_at.desc()).limit(5).all()

    # Search spools by QR code, vendor, notes, or filament info
    spools = db.query(Spool).outerjoin(FilamentLibrary, Spool.filament_id == FilamentLibrary.id).filter(
        (Spool.qr_code.ilike(query)) |
        (Spool.vendor.ilike(query)) |
        (Spool.notes.ilike(query)) |
        (FilamentLibrary.brand.ilike(query)) |
        (FilamentLibrary.name.ilike(query)) |
        (FilamentLibrary.material.ilike(query))
    ).limit(5).all()

    # Search printers
    printers = db.query(Printer).filter(
        Printer.name.ilike(query)
    ).limit(5).all()

    return {
        "models": [{"id": m.id, "name": m.name, "type": "model"} for m in models],
        "jobs": [{"id": j.id, "name": j.item_name, "status": j.status.value if j.status else None, "type": "job"} for j in jobs],
        "spools": [{"id": s.id, "name": f"{s.filament.brand} {s.filament.name}" if s.filament else (s.vendor or f"Spool #{s.id}"), "qr_code": s.qr_code, "type": "spool"} for s in spools],
        "printers": [{"id": p.id, "name": p.name, "type": "printer"} for p in printers],
    }
