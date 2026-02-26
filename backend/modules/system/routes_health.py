"""System health routes — health check, license management, and system config (spoolman, blackout)."""

import json
import logging
import os
import pathlib as _pathlib
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from pydantic import BaseModel as PydanticBaseModel, field_validator
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from core.db import get_db
from core.rbac import require_role
from modules.system.schemas import HealthCheck
from core.config import settings
from license_manager import get_license, save_license_file, get_installation_id

log = logging.getLogger("odin.api")
router = APIRouter()

_version_file = _pathlib.Path(__file__).parent.parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    try:
        from main import __version__  # noqa: F811
    except ImportError:
        __version__ = "0.0.0"


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
        except Exception:
            pass

    return HealthCheck(
        status="ok",
        version=__version__,
        database=settings.database_url.split("///")[-1],
        spoolman_connected=spoolman_ok
    )


# ============== License ==============

@router.get("/license", tags=["License"])
def get_license_info():
    """Get current license status. No auth required so frontend can check tier."""
    license_info = get_license()
    return license_info.to_dict()


@router.post("/license/upload", tags=["License"])
async def upload_license(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin")),
):
    """Upload a license file. Admin only."""
    content = await file.read()
    license_text = content.decode("utf-8").strip()

    import json as _json
    try:
        license_json = _json.loads(license_text)
        if "payload" in license_json and "signature" in license_json:
            license_text = license_json["payload"] + "." + license_json["signature"]
    except (ValueError, KeyError):
        pass

    parts = license_text.split(".")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid license file format")

    path = save_license_file(license_text)
    license_info = get_license()
    if license_info.error:
        import os as _os
        _os.remove(path)
        raise HTTPException(status_code=400, detail=license_info.error)

    return {
        "status": "activated", "tier": license_info.tier,
        "licensee": license_info.licensee, "expires_at": license_info.expires_at,
    }


@router.delete("/license", tags=["License"])
def remove_license(current_user: dict = Depends(require_role("admin"))):
    """Remove the license file (revert to Community tier). Admin only."""
    import os as _os
    from license_manager import LICENSE_DIR, LICENSE_FILENAME
    license_path = _os.path.join(LICENSE_DIR, LICENSE_FILENAME)
    if _os.path.exists(license_path):
        _os.remove(license_path)
    import license_manager
    license_manager._cached_license = None
    license_manager._cached_mtime = 0
    return {"status": "removed", "tier": "community"}


@router.get("/license/installation-id", tags=["License"])
def get_license_installation_id(current_user: dict = Depends(require_role("admin"))):
    """Return the installation ID for this ODIN instance. Admin only."""
    return {"installation_id": get_installation_id()}


class LicenseActivateRequest(PydanticBaseModel):
    key: str


@router.post("/license/activate", tags=["License"])
async def activate_license(
    request: LicenseActivateRequest,
    current_user: dict = Depends(require_role("admin")),
):
    """Activate a license by key via the license server. Admin only."""
    license_server_url = os.environ.get("LICENSE_SERVER_URL")
    if not license_server_url:
        raise HTTPException(status_code=503, detail="LICENSE_SERVER_URL is not configured. Set this environment variable to enable online activation.")
    if license_server_url.startswith("http://") and not license_server_url.startswith("http://localhost") and not license_server_url.startswith("http://127."):
        license_server_url = "https://" + license_server_url[7:]
    installation_id = get_installation_id()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{license_server_url}/api/activate",
                json={"key": request.key, "installation_id": installation_id},
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach license server: {str(e)}")

    if resp.status_code != 200:
        detail = resp.json().get("error", "Activation failed") if resp.headers.get("content-type", "").startswith("application/json") else f"License server returned {resp.status_code}"
        raise HTTPException(status_code=resp.status_code, detail=detail)

    data = resp.json()
    license_content = data.get("license_file")
    if not license_content:
        raise HTTPException(status_code=502, detail="License server returned no license file")

    path = save_license_file(license_content)
    import license_manager
    license_manager._cached_license = None
    license_manager._cached_mtime = 0
    license_info = get_license()

    if license_info.error:
        os.remove(path)
        raise HTTPException(status_code=400, detail=license_info.error)

    return {
        "status": "activated", "tier": license_info.tier,
        "licensee": license_info.licensee, "expires_at": license_info.expires_at,
        "installation_id": installation_id,
    }


@router.get("/license/activation-request", tags=["License"])
def get_activation_request(current_user: dict = Depends(require_role("admin"))):
    """Generate a downloadable activation request file for offline binding. Admin only."""
    import socket
    from starlette.responses import Response as _Response

    payload = {
        "installation_id": get_installation_id(),
        "hostname": socket.gethostname(),
        "odin_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    content = json.dumps(payload, indent=2)
    return _Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="odin-activation-request.json"'},
    )


# ============== Config ==============

class ConfigUpdate(PydanticBaseModel):
    spoolman_url: Optional[str] = None
    blackout_start: Optional[str] = None
    blackout_end: Optional[str] = None

    @field_validator('spoolman_url')
    @classmethod
    def validate_url(cls, v):
        if v is None or v == '':
            return v
        url_pattern = re.compile(
            r'^https?://'
            r'[a-zA-Z0-9]+'
            r'[a-zA-Z0-9.-]*'
            r'(:\d+)?'
            r'(/.*)?$'
        )
        if not url_pattern.match(v):
            raise ValueError('Invalid URL format. Must be http:// or https://')
        return v

    @field_validator('blackout_start', 'blackout_end')
    @classmethod
    def validate_time(cls, v):
        if v is None:
            return v
        time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$')
        if not time_pattern.match(v):
            raise ValueError('Invalid time format. Use HH:MM (e.g., 22:30)')
        return v


ALLOWED_CONFIG_KEYS = {'SPOOLMAN_URL', 'BLACKOUT_START', 'BLACKOUT_END'}


@router.get("/config", tags=["Config"])
def get_config(current_user: dict = Depends(require_role("viewer"))):
    """Get current configuration."""
    return {
        "spoolman_url": settings.spoolman_url,
        "blackout_start": settings.blackout_start,
        "blackout_end": settings.blackout_end,
    }


@router.put("/config", tags=["Config"])
def update_config(config: ConfigUpdate, current_user: dict = Depends(require_role("admin"))):
    """Update configuration. Writes to .env file."""
    env_path = os.environ.get('ENV_FILE_PATH', '/data/.env')
    env_vars = {}
    try:
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    key, val = line.strip().split('=', 1)
                    env_vars[key] = val
    except FileNotFoundError:
        pass

    if config.spoolman_url is not None:
        env_vars['SPOOLMAN_URL'] = config.spoolman_url
    if config.blackout_start is not None:
        env_vars['BLACKOUT_START'] = config.blackout_start
    if config.blackout_end is not None:
        env_vars['BLACKOUT_END'] = config.blackout_end

    import tempfile
    dir_path = os.path.dirname(env_path) or '.'
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.env.tmp')
        with os.fdopen(fd, 'w') as f:
            for key, val in env_vars.items():
                f.write(f"{key}={val}\n")
        os.replace(tmp_path, env_path)
    except Exception:
        if 'tmp_path' in dir() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return {"success": True, "message": "Config updated. Restart backend to apply changes."}


@router.get("/spoolman/test", tags=["Spoolman"])
async def test_spoolman_connection(current_user: dict = Depends(require_role("admin"))):
    """Test Spoolman connection. Admin only."""
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
