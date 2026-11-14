"""System health routes — health checks and license management.

System config (Spoolman, blackout) is in routes_config; "Refresh license" and
its last answer are in routes_license. A cancelled or timed-out
license POST may still complete at the portal: the synchronous outbound worker
cannot be interrupted. Reconcile portal state before retrying a mutation.
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from modules.system.routes_health_models import (
    LicenseActivateRequest, TrialRequestBody, OfflineActivationRequestBody, LicenseUnactivateRequest, OfflineUnactivationRequestBody
)
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from core.spoolman_settings import get_spoolman_url
from core.outbound_http import pinned_async_request
from core import deploy_freeze
from core.db import get_db
from core.schema import schema_fingerprint, validate_schema
from core.schema.bootstrap import import_all_models
from core.dependencies import log_audit
from core.rbac import require_role, require_superadmin
from modules.system.db_guardian_status import guardian_health
from modules.system.schemas import HealthCheck
from core.config import settings
from license_manager import (
    get_license, save_license_file, verify_license_text, normalize_license_upload, get_installation_id,
    get_device_keypair, sign_license_challenge, require_license_mutation_enabled, install_portal_license,
    quarantine_installed_license,
)

log = logging.getLogger("odin.api")
router = APIRouter()

from modules.system.health_version import backend_version

__version__ = backend_version()


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

    from modules.system.audit_integrity_job import health as audit_health
    from core.db import current_engine
    return HealthCheck(
        version=__version__,
        database=settings.database_url.split("///")[-1],
        spoolman_connected=spoolman_ok,
        dispatch_freeze=deploy_freeze.active(),
        **guardian_health(settings.database_url),
        audit_integrity={'status': 'ok' if audit_health(current_engine())['status'] in ('ok', 'not_licensed') else 'attention'},
    )


# v1.8.8: cached license-server reachability probe. We hit
# `${license_server_url}/api/v1/health` (or root if 404) and cache the
# outcome for 10 minutes. Only an authenticated admin may initiate this
# outbound probe; public health checks never call it.
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

    # Validate and connect under the same DNS pin in the HTTP worker.
    reachable = False
    detail = ""
    from core.itar import ItarOutboundBlocked
    try:
        for path in ("/api/v1/health", "/health", "/"):
            try:
                resp = await pinned_async_request("GET", f"{base}{path}", timeout=5)
                if resp.status_code < 500:
                    reachable = True
                    detail = f"HTTP {resp.status_code} on {path}"
                    break
            except ItarOutboundBlocked:
                raise
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
async def license_server_status(current_user: dict = Depends(require_role("admin"))):
    """Cached probe of the configured license server.

    Returns reachability + a short detail string + when the probe was
    last run. Admin only: requesting this endpoint may initiate outbound
    traffic. Locally signed licenses keep working when the server is down.
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
    """Get current license status. No auth required so frontend can check tier.

    The portal's last refresh answer (billing state) is not part of this public
    answer; administrators read it from ``GET /license/last-refresh``.
    """
    license_info = get_license(persist_clock=False)
    result = license_info.to_public_dict()
    from core.license_lock import enabled
    result['fleet_lock_rollout_enabled'] = enabled()
    return result


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
            from core.license_lock import enabled, reconcile
            if enabled(): reconcile(db)
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
    from core.license_lock import remove_installed_license
    remove_installed_license(db, current_user['id'], 'license.removed',
                             details='License removed, reverted to community tier')
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
    from core.itar import ItarOutboundBlocked
    try:
        resp = await pinned_async_request(
            "GET", f"{license_server_url}/api/v1/challenge",
            params={"installation_id": installation_id}, timeout=15,
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
    db: Session = Depends(get_db),
):
    """Activate a license by key via the license server. Admin only."""
    license_server_url = settings.license_server_url
    if license_server_url.startswith("http://") and not license_server_url.startswith("http://localhost") and not license_server_url.startswith("http://127."):
        license_server_url = "https://" + license_server_url[7:]
    if request.binding_protocol not in (None, "device-v1"):
        raise HTTPException(status_code=400, detail="Unsupported binding protocol")
    current = get_license()
    bound_requested = request.binding_protocol == "device-v1" or (current.device_bound and current.key == request.key)
    installation_id = get_installation_id()

    # S1: bind this device's Ed25519 public key to the activation. On
    # first install this generates a fresh keypair; subsequent activate
    # calls reuse it. Without the bound pubkey, later unactivate /
    # reactivate calls from anyone else who learns the (key, install_id)
    # pair can't succeed — they don't hold the private key.
    _priv, device_pubkey = get_device_keypair()

    async def activate_attempt(bound_requested):
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
        from core.itar import ItarOutboundBlocked
        try:
            resp = await pinned_async_request(
                "POST", f"{license_server_url}/api/v1/activate", timeout=15,
                json={
                    "key": request.key,
                    "installation_id": installation_id,
                    "device_pubkey": device_pubkey,
                    **({"paid_binding": "device-v1"} if bound_requested else {}),
                    **proof,
                },
            )
        except ItarOutboundBlocked as ite:
            raise HTTPException(status_code=502, detail=f"License server blocked by ITAR: {ite}")
        except Exception as e:
            log.error("License activation failed — could not reach license server: %s", e)
            raise HTTPException(status_code=502, detail="Could not reach license server. Check network connectivity and LICENSE_SERVER_URL.")

        from .license_portal import activation_response
        return activation_response(resp)

    try:
        data = await activate_attempt(bound_requested)
    except HTTPException as exc:
        if bound_requested or not isinstance(exc.detail, dict) or exc.detail.get("code") != "BINDING_REQUIRED":
            raise
        # The portal chooses binding for new purchases. A second attempt gets
        # fresh proof, and must pass the same paid device-bound verification.
        bound_requested = True
        data = await activate_attempt(bound_requested)
    license_content = data.get("license_file")
    if not license_content:
        raise HTTPException(status_code=502, detail="License server returned no license file")
    if bound_requested:
        candidate = verify_license_text(license_content.strip()) if isinstance(license_content, str) else None
        if candidate is None or not candidate.valid or not candidate.device_bound or candidate.trial:
            raise HTTPException(status_code=502, detail="The portal did not return a valid device-v1 paid license. Your installed license is unchanged. Update the portal or contact support.")
    def publish_activation(candidate):
        from core.license_lock import enabled, reconcile
        if enabled():
            reconcile(db)
            log_audit(db, 'license.activated', 'system', details={'tier': candidate.tier})
            db.commit()
    license_info = install_portal_license(license_content, after_install=publish_activation)

    return {
        "status": "activated", "tier": license_info.tier,
        "licensee": license_info.licensee, "expires_at": license_info.expires_at,
        "installation_id": installation_id,
    }




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
    from core.itar import ItarOutboundBlocked
    try:
        resp = await pinned_async_request(
            "POST", f"{license_server_url}/api/v1/trial", json={"request": trial_request}, timeout=15,
        )
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
            **({"paid_binding": "device-v1"} if body.binding_protocol else {}),
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
    current_license = get_license()
    license_key = (request and request.key) or current_license.key
    if not license_key:
        raise HTTPException(status_code=400, detail="No license key available. Provide a key or ensure current license contains one.")
    # Known local conflicts are refused here: the portal frees the grant before the local step.
    from core.license_lock import refuse_known_unactivation_conflict
    refuse_known_unactivation_conflict(request and request.key, current_license)

    installation_id = get_installation_id()

    # S1: fetch challenge, sign with device key, present both to server.
    nonce = await _fetch_license_challenge(license_server_url, installation_id)
    signature = sign_license_challenge("unactivate", license_key, installation_id, nonce)

    # v1.8.9 (codex pass 13): DNS-pinned, no env proxy.
    from core.itar import ItarOutboundBlocked
    try:
        resp = await pinned_async_request(
            "POST", f"{license_server_url}/api/v1/unactivate", timeout=15,
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

    from core.license_lock import remove_installed_license
    remove_installed_license(db, current_user['id'], 'license.unactivated',
                             details={'key': license_key[:4] + '...'},
                             expected_key=license_key, expected_sha256=current_license.license_sha256)
    return {"status": "unactivated", "tier": "community"}


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
    from core.license_lock import remove_installed_license
    remove_installed_license(db, current_user['id'], 'license.offline_unactivation_requested',
                             expected_key=current_license.key, expected_sha256=current_license.license_sha256)
    return _Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="odin-unactivation-request.json"'},
    )

