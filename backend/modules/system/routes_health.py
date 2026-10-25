"""System health routes — health check and license management. System config (Spoolman, blackout) is in routes_config."""

import json
import logging
import os
import pathlib as _pathlib
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from core.spoolman_settings import get_spoolman_url
from core.db import get_db
from core.schema import schema_fingerprint, validate_schema
from core.schema.bootstrap import import_all_models
from core.dependencies import log_audit
from core.rbac import require_role, require_superadmin
from modules.system.schemas import HealthCheck
from core.config import settings
from license_manager import (
    get_license, save_license_file, verify_license_text, normalize_license_upload, get_installation_id,
    get_device_keypair, sign_license_challenge, require_license_mutation_enabled, install_portal_license,
    quarantine_installed_license,
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

@router.get("/version", tags=["System"])
async def version():
    """Return backend version. Unauthenticated; MCP clients call this
    on first tool invocation to decide whether to forward X-Dry-Run
    safely — pre-1.9.0 backends silently execute on un-retrofitted
    routes, so the MCP client refuses dry-run against older servers.

    Response: {"version": "1.9.0"} — semver string from the VERSION file.
    """
    return {"version": __version__}


@router.get("/health", response_model=HealthCheck, tags=["System"])
async def health_check():
    """Check API health and connectivity."""
    spoolman_ok = False
    try:
        spoolman_url = get_spoolman_url()
    except Exception as exc:  # the health probe must answer even when the database is unavailable
        log.warning("Health check could not read the Spoolman URL (%s)", type(exc).__name__)
        spoolman_url = ""
    if spoolman_url:
        from fastapi.concurrency import run_in_threadpool
        from core.spoolman_client import spoolman_healthy
        spoolman_ok = await run_in_threadpool(spoolman_healthy, 5)

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

    # v1.8.9 (codex pass 13): DNS-pinned + trust_env=should_trust_env() outbound.
    # The earlier enforce_request_destination check was TOCTOU; the
    # proxy-trusting default httpx client could also bypass the pin.
    reachable = False
    detail = ""
    from core.itar import pin_for_request, ItarOutboundBlocked, should_trust_env
    try:
        with pin_for_request(base):
            async with httpx.AsyncClient(timeout=5, trust_env=should_trust_env()) as client:
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
    try:
        import_all_models()
        validation = validate_schema(db.connection())
        fingerprint = schema_fingerprint(db.connection())
    except Exception as e:
        log.error(f"/health/ready: schema validation failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={"ready": False, "reason": f"schema:{type(e).__name__}"},
        )
    # Public readiness carries only a count; reasons are in the admin diagnostics and backups list.
    from modules.system.postgres_backup_service import quarantined_restore_count
    quarantined = quarantined_restore_count(db.bind.dialect.name)
    return {"ready": True, "version": __version__, "dialect": db.bind.dialect.name,
            "schema_fingerprint": fingerprint, "notices": {"restore_quarantined": quarantined}, **validation}


# ============== License ==============

@router.get("/license", tags=["License"])
def get_license_info():
    """Get current license status. No auth required so frontend can check tier."""
    license_info = get_license()
    return license_info.to_public_dict()


@router.post("/license/upload", tags=["License"])
async def upload_license(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
):
    """Upload a license file. Admin only."""
    content = await file.read(1024 * 1024 + 1)
    license_text = normalize_license_upload(content)

    license_info = verify_license_text(license_text)
    if not license_info.valid or license_info.error:
        raise HTTPException(status_code=400, detail=license_info.error or "Invalid or expired license file")
    def audit_installation(_candidate):
        try:
            log_audit(db, "license.uploaded", "system", details={"tier": license_info.tier, "licensee": license_info.licensee})
            db.commit()
        except Exception:
            db.rollback()
            raise
    install_portal_license(license_text, after_install=audit_installation)
    return {
        "status": "activated", "tier": license_info.tier,
        "licensee": license_info.licensee, "expires_at": license_info.expires_at,
    }


@router.delete("/license", tags=["License"])
def remove_license(
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
):
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
def get_license_installation_id(current_user: dict = Depends(require_role("admin"))):
    """Return the installation ID (and, once it exists, the short device-key fingerprint) to an administrator.

    The portal's trial confirmation email and page show the same two values,
    so an admin can check that a trial link belongs to this server.
    """
    import license_manager as _lm
    fingerprint = _lm.device_fingerprint()
    return {"installation_id": get_installation_id(),
            "device_fingerprint": _lm.short_fingerprint(fingerprint) if fingerprint else None,
            "license_error": get_license().error}


class LicenseActivateRequest(PydanticBaseModel):
    key: str
    binding_protocol: str | None = None
    convert_to_device_binding: bool = False
    confirm_purchase_wide: bool = False

    # None preserves ordinary legacy refresh; device-v1 must never downgrade.



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
    from core.itar import pin_for_request, ItarOutboundBlocked, should_trust_env
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=should_trust_env()) as client:
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
    try:
        answer = resp.json()
    except Exception:
        answer = None
    nonce = answer.get("nonce") if isinstance(answer, dict) else None
    if not isinstance(nonce, str) or not nonce:
        raise HTTPException(status_code=502, detail="License server returned no nonce.")
    return nonce


@router.post("/license/activate", tags=["License"])
async def activate_license(
    request: LicenseActivateRequest,
    current_user: dict = Depends(require_superadmin(scope="write")),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
):
    """Activate a license by key via the license server. Admin only."""
    license_server_url = settings.license_server_url
    if license_server_url.startswith("http://") and not license_server_url.startswith("http://localhost") and not license_server_url.startswith("http://127."):
        license_server_url = "https://" + license_server_url[7:]
    if request.binding_protocol not in (None, "device-v1"):
        raise HTTPException(status_code=400, detail="Unsupported binding protocol")
    current = get_license()
    bound_requested = request.binding_protocol == "device-v1" or (current.device_bound and current.key == request.key)
    if request.convert_to_device_binding and not (bound_requested and request.confirm_purchase_wide):
        raise HTTPException(status_code=400, detail="Confirm purchase-wide conversion and device-v1 binding first")
    installation_id = get_installation_id()

    # S1: bind this device's Ed25519 public key to the activation. On
    # first install this generates a fresh keypair; subsequent activate
    # calls reuse it. Without the bound pubkey, later unactivate /
    # reactivate calls from anyone else who learns the (key, install_id)
    # pair can't succeed — they don't hold the private key.
    _priv, device_pubkey = get_device_keypair()

    # Explicit device-v1 and already-bound refreshes require fresh proof.
    # Ordinary legacy activation retains compatibility with older portals.
    proof = {}
    try:
        nonce = await _fetch_license_challenge(license_server_url, installation_id)
        proof = {"nonce": nonce,
                 "bootstrap_signature": sign_license_challenge("activate-bootstrap", request.key, installation_id, nonce)}
    except Exception as exc:
        if bound_requested:
            raise HTTPException(status_code=502, detail={"code": "PROOF_REQUIRED", "message": "Device binding needs a fresh portal challenge. Check connectivity or use offline activation. Your installed license is unchanged."}) from exc
        log.warning("License activation: no usable challenge from the license server (%s); sending without proof of possession",
                    getattr(exc, "status_code", type(exc).__name__))

    # v1.8.9 (codex pass 13): DNS-pinned, no env proxy.
    from core.itar import pin_for_request, ItarOutboundBlocked, should_trust_env
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=should_trust_env()) as client:
                resp = await client.post(
                    f"{license_server_url}/api/v1/activate",
                    json={
                        "key": request.key,
                        "installation_id": installation_id,
                        "device_pubkey": device_pubkey,
                        **({"binding_protocol": "device-v1"} if bound_requested else {}),
                        **({"convert_to_device_binding": True, "confirm_purchase_wide": True} if request.convert_to_device_binding else {}),
                        **proof,
                    },
                )
    except ItarOutboundBlocked as ite:
        raise HTTPException(status_code=502, detail=f"License server blocked by ITAR: {ite}")
    except Exception as e:
        log.error("License activation failed — could not reach license server: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server. Check network connectivity and LICENSE_SERVER_URL.")

    from .license_portal import activation_response
    data = activation_response(resp)
    license_content = data.get("license_file")
    if bound_requested:
        candidate = verify_license_text(license_content.strip()) if isinstance(license_content, str) else None
        if candidate is None or not candidate.valid or not candidate.device_bound or candidate.trial:
            raise HTTPException(status_code=502, detail="The portal did not return a valid device-v1 paid license. Your installed license is unchanged. Update the portal or contact support.")
    license_info = install_portal_license(license_content)

    return {
        "status": "activated", "tier": license_info.tier,
        "licensee": license_info.licensee, "expires_at": license_info.expires_at,
        "installation_id": installation_id,
    }


class TrialRequestBody(PydanticBaseModel):
    email: str
    offline: bool = False


@router.post("/license/trial-request", tags=["License"])
async def request_pro_trial(
    body: TrialRequestBody,
    current_user: dict = Depends(require_superadmin(scope="write")),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
):
    """Start a 14-day Pro trial from inside ODIN. Superadmin only.

    Builds a trial request signed with this installation's device key (the
    key activation binds) for the admin's email. Online it is POSTed to the
    licensing server, which emails a confirmation link; offline it is
    returned as a request code to paste on the portal's trial page (valid
    for 15 minutes). The key itself is shown once on the portal after the
    email is confirmed and is activated here like any key.
    """
    import license_manager as _lm
    if get_license().valid:
        raise HTTPException(status_code=409, detail="This server already has a valid license.")
    try:
        trial_request = _lm.build_trial_request(body.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # What the confirmation email and page will show: the admin checks them against these.
    identity = {"installation_id": trial_request["installation_id"],
                "device_fingerprint": _lm.short_fingerprint(_lm.device_fingerprint(trial_request["device_pubkey"]))}
    if body.offline:
        return {"request_code": _lm.trial_request_code(trial_request),
                "expires_in_minutes": _lm.TRIAL_REQUEST_MAX_AGE_MINUTES, **identity}

    license_server_url = settings.license_server_url
    if license_server_url.startswith("http://") and not license_server_url.startswith("http://localhost") and not license_server_url.startswith("http://127."):
        license_server_url = "https://" + license_server_url[7:]
    from core.itar import pin_for_request, ItarOutboundBlocked, should_trust_env
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=should_trust_env()) as client:
                resp = await client.post(f"{license_server_url}/api/v1/trial", json={"request": trial_request})
    except ItarOutboundBlocked as ite:
        raise HTTPException(status_code=502, detail=f"License server blocked by ITAR: {ite}")
    except Exception as e:
        log.error("Trial request failed — could not reach license server: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail="Could not reach the license server. Use the request code instead.")
    try:
        answer = resp.json()
    except Exception:
        answer = None
    if not isinstance(answer, dict):
        raise HTTPException(status_code=502, detail={"message": f"The license server sent an unreadable answer ({resp.status_code}).",
                                                     "portal_status": resp.status_code, "portal_code": None})
    if resp.status_code == 202 and answer.get("status") == "request_received":
        return {"status": "request_sent", **identity}
    portal_code = answer.get("code") if isinstance(answer.get("code"), str) else None
    message = answer.get("error") if isinstance(answer.get("error"), str) else "The license server refused the trial request."
    # 401/403 from the portal concern this server's signed request, not the
    # admin's session: never pass them through (the UI would sign the admin out).
    status = resp.status_code if resp.status_code in (400, 409, 429, 503) else 502
    raise HTTPException(status_code=status, detail={"message": message, "portal_status": resp.status_code, "portal_code": portal_code})


class OfflineActivationRequestBody(PydanticBaseModel):
    key: str
    nonce: str  # issued by GET /api/v1/challenge on the licensing server
    binding_protocol: str | None = None


@router.post("/license/activation-request", tags=["License"])
def build_activation_request(
    body: OfflineActivationRequestBody,
    current_user: dict = Depends(require_superadmin(scope="write")),
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

    if not body.key.strip() or not body.nonce.strip():
        raise HTTPException(status_code=400, detail="A license key and a challenge nonce from the licensing server are required.")
    if body.binding_protocol not in (None, "device-v1"):
        raise HTTPException(status_code=400, detail="Unsupported binding protocol")
    body = OfflineActivationRequestBody(key=body.key.strip(), nonce=body.nonce.strip(), binding_protocol=body.binding_protocol)
    installation_id = get_installation_id()
    _priv, device_pubkey = get_device_keypair()
    bootstrap_signature = sign_license_challenge(
        "activate-bootstrap", body.key, installation_id, body.nonce,
    )

    payload = {
        "key": body.key,
        "activation_request": {
            "installation_id": installation_id,
            **({"binding_protocol": "device-v1"} if body.binding_protocol else {}),
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
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
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
    from core.itar import pin_for_request, ItarOutboundBlocked, should_trust_env
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=should_trust_env()) as client:
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

    # Remove local license file and clear the cache
    from license_manager import LICENSE_DIR, LICENSE_FILENAME
    import license_manager
    if os.path.exists(license_path := os.path.join(LICENSE_DIR, LICENSE_FILENAME)):
        os.remove(license_path)
    license_manager._cached_license = None
    license_manager._cached_mtime = 0

    log_audit(db, "license.unactivated", "system", details={"key": license_key[:4] + "..."})
    db.commit()
    return {"status": "unactivated", "tier": "community"}


@router.post("/license/reactivate", tags=["License"])
async def reactivate_license(
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
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
    from core.itar import pin_for_request, ItarOutboundBlocked, should_trust_env
    try:
        with pin_for_request(license_server_url):
            async with httpx.AsyncClient(timeout=15, trust_env=should_trust_env()) as client:
                resp = await client.post(
                    f"{license_server_url}/api/v1/reactivate",
                    json={
                        "key": current_license.key,
                        **({"binding_protocol": "device-v1"} if current_license.device_bound else {}),
                        "installation_id": installation_id,
                        "nonce": nonce,
                        "signature": signature,
                    },
            )
    except Exception as e:
        log.error("License reactivation failed — could not reach license server: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach license server.")

    from .license_portal import activation_response
    data = activation_response(resp)
    license_content = data.get("license_file")
    if not license_content:
        raise HTTPException(status_code=502, detail="License server returned no license file")

    if current_license.device_bound:
        candidate = verify_license_text(license_content.strip()) if isinstance(license_content, str) else None
        if candidate is None or not candidate.valid or not candidate.device_bound or candidate.trial:
            raise HTTPException(status_code=502, detail="The portal returned an invalid device-bound refresh. Your installed license is unchanged; contact support.")
    def audit_refresh(candidate):
        log_audit(db, "license.reactivated", "system", details={"tier": candidate.tier, "licensee": candidate.licensee})
        db.commit()
    license_info = install_portal_license(license_content, after_install=audit_refresh)
    return {
        "status": "reactivated",
        "tier": license_info.tier,
        "licensee": license_info.licensee,
        "expires_at": license_info.expires_at,
    }


class OfflineUnactivationRequestBody(PydanticBaseModel):
    nonce: str  # issued by GET /api/v1/challenge on the licensing server


@router.post("/license/unactivation-request", tags=["License"])
def get_unactivation_request(
    body: OfflineUnactivationRequestBody,
    current_user: dict = Depends(require_superadmin(scope="write")),
    db: Session = Depends(get_db),
    license_mutation_allowed: None = Depends(require_license_mutation_enabled),
):
    """Signed offline unactivation request over a portal challenge nonce (proof of possession). Admin only."""
    import socket
    from starlette.responses import Response as _Response

    current_license = get_license()
    if not current_license.key:
        raise HTTPException(status_code=400, detail="Current license has no key. Cannot generate unactivation request.")
    nonce = body.nonce.strip()
    if not nonce:
        raise HTTPException(status_code=400, detail="A challenge nonce from the licensing server is required.")

    installation_id = get_installation_id()
    payload = {
        "installation_id": installation_id,
        "hostname": socket.gethostname(),
        "key": current_license.key,
        "odin_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nonce": nonce,
        "signature": sign_license_challenge("unactivate", current_license.key, installation_id, nonce),
    }
    content = json.dumps(payload, indent=2)
    try:  # hands the seat over: this install stops using the license now (as online /unactivate)
        quarantined = quarantine_installed_license()
    except OSError:
        raise HTTPException(status_code=503, detail="License could not be removed; no unactivation request was issued")
    log_audit(db, "license.offline_unactivation_requested", "system", details={"quarantined": os.path.basename(quarantined or "")})
    db.commit()
    return _Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="odin-unactivation-request.json"'},
    )
