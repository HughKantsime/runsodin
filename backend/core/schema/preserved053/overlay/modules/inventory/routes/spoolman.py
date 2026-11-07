"""Spoolman integration endpoints.

The push-consumption helper lives in `modules/inventory/services.py` so
other modules (jobs_lifecycle.py) can import it through the sanctioned
`.services import` cross-module allowlist entry. Do NOT import that
helper from here — go through services.py.
"""

import logging
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.spoolman_settings import get_spoolman_url
from core.db import get_db
from core.config import settings
from core.rbac import require_role, require_action
from modules.printers.schemas import SpoolmanSpool, SpoolmanSyncResult

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/spoolman", tags=["Spoolman"])


@router.post("/sync", response_model=SpoolmanSyncResult, tags=["Spoolman"])
async def sync_spoolman(current_user: dict = Depends(require_action("spools.edit", scope="write:spools")), db: Session = Depends(get_db)):
    """Sync filament data from Spoolman."""
    if not get_spoolman_url(db):
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")
    spools = await _fetch_spools()
    # For now, just return what we found - actual slot matching would need user mapping
    return SpoolmanSyncResult(
        success=True,
        spools_found=len(spools),
        slots_updated=0,
        message=f"Found {len(spools)} spools in Spoolman. Use the UI to assign spools to printer slots.",
    )


async def _fetch_spools() -> list:
    """Validated, pinned GET of Spoolman's spools (core.spoolman_client), projected."""
    from fastapi.concurrency import run_in_threadpool
    from core.spoolman_client import SpoolmanURLRefused, fetch_spools
    try:
        return await run_in_threadpool(fetch_spools, 10)
    except SpoolmanURLRefused as refused:
        raise HTTPException(status_code=400, detail=str(refused)) from None
    except Exception as e:
        log.error("Spoolman connection failed: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail="Failed to connect to Spoolman. Check Spoolman URL in settings.") from None


@router.get("/spools", response_model=List[SpoolmanSpool], tags=["Spoolman"])
async def list_spoolman_spools(current_user: dict = Depends(require_role("viewer", scope="read:spools")), db: Session = Depends(get_db)):
    """List available spools from Spoolman."""
    if not get_spoolman_url(db):
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")
    spools = []
    for s in await _fetch_spools():
        filament = s["filament"]
        spools.append(SpoolmanSpool(
            id=s.get("id"),
            filament_name=filament.get("name") or "Unknown",
            filament_type=filament.get("material") or "PLA",
            color_name=filament.get("color_name"),
            color_hex=filament.get("color_hex"),
            remaining_weight=s.get("remaining_weight"),
        ))
    return spools


@router.get("/filaments", tags=["Spoolman"])
async def get_spoolman_filaments(current_user: dict = Depends(require_role("viewer", scope="read:spools")), db: Session = Depends(get_db)):
    """Fetch all filament types from Spoolman (only the fields ODIN uses)."""
    from fastapi.concurrency import run_in_threadpool
    from core.spoolman_client import SpoolmanURLRefused, filament_summary, spoolman_request
    if not get_spoolman_url(db):
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")
    try:
        response = await run_in_threadpool(lambda: spoolman_request("GET", "/api/v1/filament", timeout=10))
        response.raise_for_status()
        data = response.json()
    except SpoolmanURLRefused as refused:
        raise HTTPException(status_code=400, detail=str(refused)) from None
    except Exception as e:
        log.error("Spoolman connection failed: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail="Failed to connect to Spoolman. Check Spoolman URL in settings.") from None
    return [filament_summary(f) for f in data if isinstance(f, dict)] if isinstance(data, list) else []
