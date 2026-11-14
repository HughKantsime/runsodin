"""System health routes — health checks and license-server status."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.spoolman_settings import get_spoolman_url
from core.outbound_http import pinned_async_request
from core import deploy_freeze
from core.db import get_db
from core.schema import schema_fingerprint, validate_schema
from core.schema.bootstrap import import_all_models
from core.rbac import require_role
from modules.system.db_guardian_status import guardian_health
from modules.system.schemas import HealthCheck
from core.config import settings

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
