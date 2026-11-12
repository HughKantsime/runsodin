"""Printer network discovery routes (Klipper/Moonraker via mDNS)."""
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rate_limit import limiter
from core.rbac import require_action
from modules.printers import moonraker_discovery

log = logging.getLogger("odin.api")
router = APIRouter()


@router.post("/printers/discover/moonraker", tags=["Printers"])
@limiter.limit("10/minute")
def discover_moonraker_printers(
    request: Request,
    current_user: dict = Depends(require_action("printers.add", scope="write:printers")),
    db: Session = Depends(get_db),
):
    """Find Klipper printers announcing _moonraker._tcp on the local network.

    Returns candidates that pass the device destination policy and are on the local
    network, marking hosts that are already printers. Without multicast access it
    returns an empty list and a plain reason, not an error. Sends no credentials.
    """
    result = moonraker_discovery.discover(db, current_user)
    log_audit(db, "printer.discovery", "printer", None, {
        "type": "moonraker",
        "offered": len(result["printers"]),
        "filtered_out": result["filtered_out"],
        "empty_reason": bool(result["reason"]),
    })
    db.commit()
    return {"printers": result["printers"], "reason": result["reason"]}
