"""Agent-surface filament-slot assignment — POST /filament-slots.

v1.9.0 Phase 2 (T2.8). The MCP `assign_spool` tool calls
`POST /api/v1/filament-slots` with `{spool_id, printer_id, ams_slot}`.
The legacy portal path is `PATCH /api/v1/printers/{id}/slots/{n}` in
`routes_crud.py`, which takes a partial-update body shape that doesn't
match what the MCP emits. Rather than push complexity onto the MCP
client (which is already published on npm), this file adds a thin
agent-friendly wrapper that binds the three MCP fields to a slot
assignment via the existing SQLAlchemy model.

Canonical Phase 2 pattern:
  - Stacked auth (require_role operator + require_any_scope agent:write).
  - is_dry_run(request) branch before any mutation.
  - OdinError envelope on 4xx.
  - next_actions emitted on success.
  - Registered in DRY_RUN_SUPPORTED_ROUTES.
"""

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import (
    AGENT_WRITE_SCOPE,
    check_org_access,
    require_any_scope,
    require_role,
)
from core.responses import build_next_actions, next_action
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot, Printer

log = logging.getLogger("odin.api")
router = APIRouter(tags=["Filament"])


class AssignSpoolRequest(PydanticBaseModel):
    """MCP assign_spool body shape. `ams_slot` optional — omit for
    single-slot (non-AMS) printers; defaults to slot_number 1."""
    spool_id: int
    printer_id: int
    ams_slot: int | None = None


@router.post("/filament-slots", tags=["Filament"])
def assign_spool_to_slot(
    body: AssignSpoolRequest,
    request: Request,
    current_user: dict = Depends(require_role("operator")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Assign a spool to a printer's AMS slot. Agent-surface v1.9.0 Phase 2.

    Body: `{spool_id, printer_id, ams_slot}`. `ams_slot` is 1-indexed
    (matches FilamentSlot.slot_number). If omitted, defaults to slot 1.
    """
    printer = db.query(Printer).filter(Printer.id == body.printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise OdinError(
            ErrorCode.printer_not_found,
            f"Printer {body.printer_id} not found",
            status=404,
        )

    spool = db.query(Spool).filter(Spool.id == body.spool_id).first()
    if not spool or not check_org_access(current_user, spool.org_id):
        raise OdinError(
            ErrorCode.spool_not_found,
            f"Spool {body.spool_id} not found",
            status=404,
        )

    slot_number = body.ams_slot if body.ams_slot is not None else 1
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == body.printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise OdinError(
            ErrorCode.not_found,
            f"Filament slot {slot_number} not found on printer {body.printer_id}",
            status=404,
        )

    previous_spool_id = slot.assigned_spool_id

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "assign_spool",
                "printer_id": body.printer_id,
                "printer_name": printer.name,
                "slot_number": slot_number,
                "new_spool_id": body.spool_id,
                "previous_spool_id": previous_spool_id,
                "spool_filament_type": (
                    spool.filament.filament_type.value
                    if getattr(spool, "filament", None)
                    and getattr(spool.filament, "filament_type", None)
                    else None
                ),
            },
            next_actions=[
                next_action(
                    "get_printer",
                    {"printer_id": body.printer_id},
                    "verify slot assignment",
                ),
                next_action(
                    "list_spools",
                    {"available_only": False},
                    "see which spool is now loaded",
                ),
            ],
            notes="Would update filament_slots.assigned_spool_id; no other side effects.",
        )

    slot.assigned_spool_id = body.spool_id
    slot.spool_confirmed = True
    slot.loaded_at = datetime.now(timezone.utc)
    log_audit(
        db,
        "slot.assigned",
        "filament_slot",
        slot.id,
        {
            "printer_id": body.printer_id,
            "slot_number": slot_number,
            "spool_id": body.spool_id,
            "previous_spool_id": previous_spool_id,
        },
    )
    db.commit()
    db.refresh(slot)

    return {
        "success": True,
        "printer_id": body.printer_id,
        "slot_number": slot_number,
        "spool_id": body.spool_id,
        "previous_spool_id": previous_spool_id,
        "next_actions": build_next_actions(
            next_action(
                "get_printer",
                {"printer_id": body.printer_id},
                "confirm slot assignment",
            ),
        ),
    }
