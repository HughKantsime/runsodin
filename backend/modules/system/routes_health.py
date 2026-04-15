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
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role, require_superadmin
from modules.system.schemas import HealthCheck
from core.config import settings
from license_manager import (
    get_license, save_license_file, get_installation_id,
    get_device_keypair, sign_license_challenge,
)

log = logging.getLogger("odin.api")
router = APIRouter()

_version_file = _pathlib.Path(__file__).parent.parent.parent.parent / "VERSION"
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
            # v1.8.9 (codex pass 13): DNS-pinned + trust_env=False.
            from core.itar import pin_for_request
            with pin_for_request(settings.spoolman_url):
                async with httpx.AsyncClient(trust_env=False) as client:
                    resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
                    spoolman_ok = resp.status_code == 200
        except Exception as e:
            log.debug(f"Spoolman health check failed: {e}")

    return HealthCheck(
        status="ok",
        version=__version__,
        database=settings.database_url.split("///")[-1],
        spoolman_connected=spoolman_ok
    )


# v1.8.8: cached license-server reachability probe. We hit
# `${license_server_url}/api/v1/health` (or root if 404) and cache the
# outcome for 10 minutes. Surfaced via /system/health and the SystemTab
# UI so operators see "license server unreachable, locally-cached
# activation still active" instead of guessing.
_LICENSE_PROBE_TTL_SECONDS = 600
_license_probe_cache = {"checked_at": 0.0, "reachable": None, "detail": ""}


async def _probe_license_server() -> dict:
    """Hit the configured license server and cache the result."""
    import time
    now = time.time()
    cached = _license_probe_cache
    if cached["reachable"] is not None and (now - cached["checked_at"]) < _LICENSE_PROBE_TTL_SECONDS:
        return {
            "reachable": cached["reachable"],
            "detail": cached["detail"],
            "checked_at": cached["checked_at"],
            "cached": True,
        }

    base = (settings.license_server_url or "").rstrip("/")
    if not base:
        cached.update({"reachable": False, "detail": "license_server_url not configured", "checked_at": now})
        return {**cached, "cached": False}

    # v1.8.9 (codex pass 13): DNS-pinned + trust_env=False outbound.
    # The earlier enforce_request_destination check was TOCTOU; the
    # proxy-trusting default httpx client could also bypass the pin.
    reachable = False
    detail = ""
    from core.itar import pin_for_request, ItarOutboundBlocked
    try:
        with pin_for_request(base):
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                for path in ("/api/v1/health", "/health", "/"):
                    try:
                        resp = await client.get(f"{base}{path}")
                        if resp.status_code < 500:
                            reachable = True
                            detail = f"HTTP {resp.status_code} on {path}"
                            break
                    except Exception:
                        continue
                if not reachable:
                    detail = "all probe paths failed"
    except ItarOutboundBlocked as exc:
        cached.update({
            "reachable": False,
            "detail": f"blocked by ITAR: {exc}",
            "checked_at": now,
        })
        return {**cached, "cached": False}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"

    cached.update({"reachable": reachable, "detail": detail, "checked_at": now})
    return {**cached, "cached": False}


@router.get("/system/license-server-status", tags=["System"])
async def license_server_status():
    """Cached probe of the configured license server.

    Returns reachability + a short detail string + when the probe was
    last run. Polled every 10 minutes by the System Health UI; locally-
    signed licenses keep working regardless, but operators want to know
    when the upstream is down so an outage doesn't get blamed on ODIN.
    """
    return await _probe_license_server()


@router.get("/health/ready", tags=["System"])
def readiness_check(db: Session = Depends(get_db)):
    """Kubernetes-style readiness probe. Returns 200 when ODIN is ready
    to serve traffic; returns 503 otherwise.

    v1.8.8: watchtower and other orchestrators can gate container
    replacement on this endpoint — pull the new image, wait for
    /health/ready=200 before killing the old container. Documented in
    the README's watchtower section.

    Checks (order matters — cheapest first):
      1. DB session round-trip (SELECT 1).
      2. A known-good migrated table is readable (users). If migrations
         haven't applied cleanly, the query raises.

    Does NOT check Spoolman / license server reachability — those are
    informational, not readiness. A network partition to an upstream
    shouldn't flap the container.
    """
    try:
        db.execute(text("SELECT 1")).fetchone()
    except Exception as e:
        log.error(f"/health/ready: DB round-trip failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={"ready": False, "reason": f"db:{type(e).__name__}"},
        )
    try:
        db.execute(text("SELECT 1 FROM users LIMIT 1")).fetchone()
    except Exception as e:
        log.error(f"/health/ready: users table not queryable: {e}")
        raise HTTPException(
            status_code=503,
            detail={"ready": False, "reason": f"migrations:{type(e).__name__}"},
        )
    return {"ready": True, "version": __version__}


# ============== License ==============

@router.get("/license", tags=["License"])
def get_license_info():
    """Get current license status. No auth required so frontend can check tier."""
    license_info = get_license()
    return license_info.to_dict()


@router.post("/license/upload", tags=["License"])
async def upload_license(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db),
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

    log_audit(db, "license.uploaded", "system", details={"tier": license_info.tier, "licensee": license_info.licensee})
    db.commit()
    return {
        "status": "activated", "tier": license_info.tier,
        "licensee": license_info.licensee, "expires_at": license_info.expires_at,
    }


@router.delete("/license", tags=["License"])
def remove_license(current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    """Remove the license file (revert to Community tier). Admin only."""
    import os as _os
    from license_manager import LICENSE_DIR, LICENSE_FILENAME
    license_path = _os.path.join(LICENSE_DIR, LICENSE_FILENAME)
    if _os.path.exists(license_path):
        _os.remove(license_path)
    import license_manager
    license_manager._cached_license = None
    license_manager._cached_mtime = 0
    log_audit(db, "license.removed", "system", details="License removed, reverted to community tier")
    db.commit()
    return {"status": "removed", "tier": "community"}


@router.get("/license/installation-id", tags=["License"])
def get_license_installation_id(current_user: dict = Depends(require_superadmin())):
    """Return the installation ID for this ODIN instance. Admin only."""
    return {"installation_id": get_installation_id()}


class LicenseActivateRequest(PydanticBaseModel):
    key: str


async def _fetch_license_challenge(license_server_url: str, installation_id: str) -> str:
    """Fetch a single-use challenge nonce from the license server.

    S1 from 2026-04-12 review: unactivate / reactivate require proof-of-
    possession, which means signing a server-issued nonce. This helper
    encapsulates the GET so the three license routes share the flow.

    v1.8.9 (codex pass 7): runtime ITAR enforcement. Boot-audit alone
    isn't enough — if the configured license_server_url's DNS shifts
    to a public address post-boot (split-horizon, config drift,
    resolver change), the boot audit can't catch it. Every call
    checks the destination before the HTTP request fires.
    """
    from core.itar import pin_for_request, ItarOutboundBlocked
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                resp = await client.get(
                    f"{license_server_url}/api/v1/challenge",
                    params={"installation_id": installation_id},
                )
    except ItarOutboundBlocked as exc:
        log.error("License challenge refused under ITAR: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="License server destination blocked by ITAR mode.",
        )
    except Exception as e:
        log.error("License challenge fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server.")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Could not get license challenge.")
    nonce = resp.json().get("nonce")
    if not nonce:
        raise HTTPException(status_code=502, detail="License server returned no nonce.")
    return nonce


@router.post("/license/activate", tags=["License"])
async def activate_license(
    request: LicenseActivateRequest,
    current_user: dict = Depends(require_superadmin()),
):
    """Activate a license by key via the license server. Admin only."""
    license_server_url = settings.license_server_url
    if license_server_url.startswith("http://") and not license_server_url.startswith("http://localhost") and not license_server_url.startswith("http://127."):
        license_server_url = "https://" + license_server_url[7:]
    installation_id = get_installation_id()

    # S1: bind this device's Ed25519 public key to the activation. On
    # first install this generates a fresh keypair; subsequent activate
    # calls reuse it. Without the bound pubkey, later unactivate /
    # reactivate calls from anyone else who learns the (key, install_id)
    # pair can't succeed — they don't hold the private key.
    _priv, device_pubkey = get_device_keypair()

    # v1.8.9 (codex pass 13): DNS-pinned, no env proxy.
    from core.itar import pin_for_request, ItarOutboundBlocked
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                resp = await client.post(
                    f"{license_server_url}/api/v1/activate",
                    json={
                        "key": request.key,
                        "installation_id": installation_id,
                        "device_pubkey": device_pubkey,
                    },
                )
    except ItarOutboundBlocked as ite:
        raise HTTPException(status_code=502, detail=f"License server blocked by ITAR: {ite}")
    except Exception as e:
        log.error("License activation failed — could not reach license server: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server. Check network connectivity and LICENSE_SERVER_URL.")

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


class OfflineActivationRequestBody(PydanticBaseModel):
    key: str
    nonce: str  # issued by GET /api/v1/challenge on the licensing server


@router.post("/license/activation-request", tags=["License"])
def build_activation_request(
    body: OfflineActivationRequestBody,
    current_user: dict = Depends(require_superadmin()),
):
    """Generate a downloadable activation request for offline binding. Admin only.

    S1 from 2026-04-12 review: offline activation now requires the admin
    to (1) fetch a challenge nonce from the license server on a separate
    internet-connected machine, (2) POST it here along with the license
    key, (3) this handler returns a signed bundle (the device's public
    key + a bootstrap signature over the canonical message), (4) admin
    uploads that bundle to /api/v1/offline on the licensing server.

    Without the bootstrap signature, an attacker who obtained the
    (key, installation_id) pair could mint an offline license on a
    different machine they control. The signature proves the request
    originated from the machine that holds this device's private key.
    """
    import socket
    from starlette.responses import Response as _Response

    installation_id = get_installation_id()
    _priv, device_pubkey = get_device_keypair()
    bootstrap_signature = sign_license_challenge(
        "activate-bootstrap", body.key, installation_id, body.nonce,
    )

    payload = {
        "key": body.key,
        "activation_request": {
            "installation_id": installation_id,
            "hostname": socket.gethostname(),
            "odin_version": __version__,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "device_pubkey": device_pubkey,
            "nonce": body.nonce,
            "bootstrap_signature": bootstrap_signature,
        },
    }
    content = json.dumps(payload, indent=2)
    return _Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="odin-activation-request.json"'},
    )


class LicenseUnactivateRequest(PydanticBaseModel):
    key: str = None  # Optional — if not provided, read from current license


@router.post("/license/unactivate", tags=["License"])
async def unactivate_license(
    request: LicenseUnactivateRequest = None,
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db),
):
    """Unactivate the current license to free the grant for another server. Admin only."""
    license_server_url = settings.license_server_url

    # Get key from request or current license
    license_key = (request and request.key) or get_license().key
    if not license_key:
        raise HTTPException(status_code=400, detail="No license key available. Provide a key or ensure current license contains one.")

    installation_id = get_installation_id()

    # S1: fetch challenge, sign with device key, present both to server.
    nonce = await _fetch_license_challenge(license_server_url, installation_id)
    signature = sign_license_challenge("unactivate", license_key, installation_id, nonce)

    # v1.8.9 (codex pass 13): DNS-pinned, no env proxy.
    from core.itar import pin_for_request, ItarOutboundBlocked
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                resp = await client.post(
                    f"{license_server_url}/api/v1/unactivate",
                    json={
                        "key": license_key,
                        "installation_id": installation_id,
                        "nonce": nonce,
                        "signature": signature,
                    },
                )
    except ItarOutboundBlocked as ite:
        raise HTTPException(status_code=502, detail=f"License server blocked by ITAR: {ite}")
    except Exception as e:
        log.error("License unactivation failed — could not reach license server: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server.")

    if resp.status_code != 200:
        detail = resp.json().get("error", "Unactivation failed") if resp.headers.get("content-type", "").startswith("application/json") else f"License server returned {resp.status_code}"
        raise HTTPException(status_code=resp.status_code, detail=detail)

    # Remove local license file
    from license_manager import LICENSE_DIR, LICENSE_FILENAME
    license_path = os.path.join(LICENSE_DIR, LICENSE_FILENAME)
    if os.path.exists(license_path):
        os.remove(license_path)

    # Clear cache
    import license_manager
    license_manager._cached_license = None
    license_manager._cached_mtime = 0

    log_audit(db, "license.unactivated", "system", details={"key": license_key[:4] + "..."})
    db.commit()
    return {"status": "unactivated", "tier": "community"}


@router.post("/license/reactivate", tags=["License"])
async def reactivate_license(
    current_user: dict = Depends(require_superadmin()),
    db: Session = Depends(get_db),
):
    """Reactivate the license to pick up tier/feature changes. Admin only."""
    license_server_url = settings.license_server_url

    current_license = get_license()
    if not current_license.key:
        raise HTTPException(status_code=400, detail="Current license has no key. Cannot reactivate.")

    installation_id = get_installation_id()

    # S1: fetch challenge, sign with device key.
    nonce = await _fetch_license_challenge(license_server_url, installation_id)
    signature = sign_license_challenge("reactivate", current_license.key, installation_id, nonce)

    # v1.8.9 (codex pass 13): DNS-pinned, no env proxy.
    from core.itar import pin_for_request, ItarOutboundBlocked
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                resp = await client.post(
                    f"{license_server_url}/api/v1/reactivate",
                    json={
                        "key": current_license.key,
                        "installation_id": installation_id,
                        "nonce": nonce,
                        "signature": signature,
                    },
            )
    except Exception as e:
        log.error("License reactivation failed — could not reach license server: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server.")

    if resp.status_code != 200:
        detail = resp.json().get("error", "Reactivation failed") if resp.headers.get("content-type", "").startswith("application/json") else f"License server returned {resp.status_code}"
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

    log_audit(db, "license.reactivated", "system", details={"tier": license_info.tier, "licensee": license_info.licensee})
    db.commit()
    return {
        "status": "reactivated",
        "tier": license_info.tier,
        "licensee": license_info.licensee,
        "expires_at": license_info.expires_at,
    }


@router.get("/license/unactivation-request", tags=["License"])
def get_unactivation_request(current_user: dict = Depends(require_superadmin())):
    """Generate a downloadable unactivation request file for offline unactivation. Admin only."""
    import socket
    from starlette.responses import Response as _Response

    current_license = get_license()
    if not current_license.key:
        raise HTTPException(status_code=400, detail="Current license has no key. Cannot generate unactivation request.")

    payload = {
        "installation_id": get_installation_id(),
        "hostname": socket.gethostname(),
        "key": current_license.key,
        "odin_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    content = json.dumps(payload, indent=2)
    return _Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="odin-unactivation-request.json"'},
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
def update_config(config: ConfigUpdate, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
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

    log_audit(db, "config.updated", "system", details={"keys": [k for k in ["spoolman_url", "blackout_start", "blackout_end"] if getattr(config, k) is not None]})
    db.commit()
    return {"success": True, "message": "Config updated. Restart backend to apply changes."}


@router.get("/spoolman/test", tags=["Spoolman"])
async def test_spoolman_connection(current_user: dict = Depends(require_superadmin())):
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
        log.warning("Spoolman connection test failed: %s", e)
        return {"success": False, "message": "Connection failed. Check Spoolman URL and network connectivity."}
