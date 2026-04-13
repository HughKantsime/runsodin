"""Spoolman integration endpoints + push-consumption helper."""

import logging
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.config import settings
from core.rbac import require_role
from core.webhook_utils import safe_post, WebhookSSRFError
from modules.printers.schemas import SpoolmanSpool, SpoolmanSyncResult

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/spoolman", tags=["Spoolman"])


def push_consumption_to_spoolman(deductions: list) -> list:
    """POST usage records to the configured Spoolman instance.

    Ships bidirectional sync (v1.8.5): ODIN already owns the authoritative
    local spool count; this pushes the consumption delta back to Spoolman
    so its inventory stays in lock-step.

    Args:
        deductions: list of dicts with at least:
            - spoolman_spool_id: int | None  (None = spool not linked to Spoolman; skip)
            - grams: float  (positive consumption amount)
            - job_id: int   (for log context)

    Returns:
        list[str] of human-readable error messages, one per failed push.
        Empty list means every linked deduction was accepted.

    Design notes:
        * Fails loud — errors are returned to the caller, which surfaces
          them via log + job.notes + dispatch_alert. No silent swallowing.
        * Uses safe_post() so the Spoolman URL (user-configured) gets the
          SSRF DNS-pin treatment. Rebinding a Spoolman hostname to an
          internal target is the attack we guard against.
        * Skips entries with spoolman_spool_id=None without raising.
          A job may involve spools that aren't linked to Spoolman; that's
          a supported state.
        * No-op when settings.spoolman_url is empty — Spoolman integration
          is disabled.
    """
    errors: list = []

    base = (settings.spoolman_url or "").rstrip("/")
    if not base:
        return errors  # integration disabled; not an error

    for d in deductions:
        spoolman_id = d.get("spoolman_spool_id")
        if not spoolman_id:
            continue  # not linked — skip silently

        grams = float(d.get("grams", 0) or 0)
        if grams <= 0:
            continue  # nothing consumed (edge case)

        url = f"{base}/api/v1/spool/{spoolman_id}/use"
        try:
            resp = safe_post(
                url,
                json={"use_weight": grams},
                timeout=5,
            )
            # Spoolman returns 200 on success. If it returns a 4xx we want
            # to know — this is usually a bad spool id or a malformed body.
            if hasattr(resp, "status_code") and resp.status_code >= 400:
                errors.append(
                    f"Spoolman spool={spoolman_id} job={d.get('job_id')} "
                    f"rejected push: HTTP {resp.status_code}"
                )
        except WebhookSSRFError as e:
            errors.append(
                f"Spoolman URL blocked by SSRF check (spool={spoolman_id}): {e}"
            )
        except Exception as e:
            errors.append(
                f"Spoolman push failed (spool={spoolman_id} job={d.get('job_id')}): {e}"
            )

    return errors


@router.post("/sync", response_model=SpoolmanSyncResult, tags=["Spoolman"])
async def sync_spoolman(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Sync filament data from Spoolman."""
    if not settings.spoolman_url:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=10)
            resp.raise_for_status()
            spools = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {e}")

    # For now, just return what we found - actual slot matching would need user mapping
    return SpoolmanSyncResult(
        success=True,
        spools_found=len(spools),
        slots_updated=0,
        message=f"Found {len(spools)} spools in Spoolman. Use the UI to assign spools to printer slots.",
    )


@router.get("/spools", response_model=List[SpoolmanSpool], tags=["Spoolman"])
async def list_spoolman_spools(current_user: dict = Depends(require_role("viewer"))):
    """List available spools from Spoolman."""
    if not settings.spoolman_url:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=10)
            resp.raise_for_status()
            spools_data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {e}")

    spools = []
    for s in spools_data:
        filament = s.get("filament", {})
        spools.append(SpoolmanSpool(
            id=s.get("id"),
            filament_name=filament.get("name", "Unknown"),
            filament_type=filament.get("material", "PLA"),
            color_name=filament.get("color_name"),
            color_hex=filament.get("color_hex"),
            remaining_weight=s.get("remaining_weight"),
        ))

    return spools


@router.get("/filaments", tags=["Spoolman"])
async def get_spoolman_filaments(current_user: dict = Depends(require_role("viewer"))):
    """Fetch all filament types from Spoolman."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{settings.spoolman_url}/api/v1/filament", timeout=10.0)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        log.error(f"Spoolman connection failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to Spoolman. Check Spoolman URL in settings.")
