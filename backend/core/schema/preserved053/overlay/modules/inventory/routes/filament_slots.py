"""Printer filament slot assignment and attention endpoints."""

import logging
from modules.inventory.slot_assignments import assign_slot
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role, require_action, check_org_access
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot, Printer
from core.interfaces.filament_ownership import ensure_local_filament_management

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/printers", tags=["Filament Slots"])


@router.post("/{printer_id}/slots/{slot_number}/assign", tags=["Spools"])
def assign_spool_to_slot(
    printer_id: int,
    slot_number: int,
    spool_id: int,
    current_user: dict = Depends(require_action("printers.slots", scope="write:printers")), db: Session = Depends(get_db)
):
    """Assign a spool to a printer slot."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    ensure_local_filament_management(printer_id)
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    assign_slot(db, slot, spool_id, current_user)
    slot.spool_confirmed = False

    db.commit()

    return {"success": True, "message": "Spool assigned, awaiting confirmation"}


@router.post("/{printer_id}/slots/{slot_number}/confirm", tags=["Spools"])
def confirm_slot_assignment(
    printer_id: int,
    slot_number: int,
    current_user: dict = Depends(require_action("printers.slots", scope="write:printers")), db: Session = Depends(get_db)
):
    """Confirm the spool assignment for a slot."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    ensure_local_filament_management(printer_id)
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    from core.tray_identity import decode
    if decode(slot.tray_observation).get('pending'):
        raise HTTPException(409, 'Confirm the observed tray identity with same or new')

    if not slot.assigned_spool_id:
        raise HTTPException(status_code=400, detail="No spool assigned to confirm")

    slot.spool_confirmed = True
    db.commit()

    return {"success": True, "message": "Spool assignment confirmed"}


@router.get("/{printer_id}/slots/needs-attention", tags=["Spools"])
def get_slots_needing_attention(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """Get slots that need spool confirmation or have mismatches."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    issues = []
    for slot in printer.filament_slots:
        slot_issues = []

        from core.tray_identity import decode
        if decode(slot.tray_observation).get('pending'):
            slot_issues.append('Spool may have changed?')

        # No spool assigned but slot has filament
        if not slot.assigned_spool_id and slot.color_hex:
            slot_issues.append("No spool assigned")

        # Spool assigned but not confirmed
        if slot.assigned_spool_id and not slot.spool_confirmed:
            slot_issues.append("Awaiting confirmation")

        # Spool assigned but type/color mismatch
        if slot.assigned_spool and slot.assigned_spool.filament:
            spool_fil = slot.assigned_spool.filament
            if slot.color_hex and spool_fil.color_hex:
                # Simple mismatch check
                if slot.color_hex.lower().replace("#", "") != spool_fil.color_hex.lower().replace("#", ""):
                    slot_issues.append(f"Color mismatch: slot={slot.color_hex}, spool={spool_fil.color_hex}")

        if slot_issues:
            issues.append({
                "slot_number": slot.slot_number,
                "issues": slot_issues,
                "current_type": slot.filament_type.value if slot.filament_type else None,
                "current_color": slot.color,
                "current_color_hex": slot.color_hex,
                "assigned_spool_id": slot.assigned_spool_id,
                "spool_confirmed": slot.spool_confirmed,
            })

    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "slots_needing_attention": len(issues),
        "slots": issues,
    }


from typing import Literal
from pydantic import BaseModel, Field, model_validator


class ConfirmTrayIdentity(BaseModel):
    observed_state: str = Field(pattern=r'^[0-9a-f]{32}$')
    decision: Literal['same', 'new']
    spool_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def target(self):
        if (self.decision == 'new') != (self.spool_id is not None):
            raise ValueError('New spool requires spool_id; same spool uses the current binding')
        return self


@router.post('/{printer_id}/slots/{slot_number}/confirm-identity', tags=['Spools'])
def confirm_tray_identity(printer_id: int, slot_number: int, body: ConfirmTrayIdentity,
    current_user: dict = Depends(require_action('printers.slots', scope='write:printers')),
    db: Session = Depends(get_db)):
    """Confirm this observation, atomically bind inventory and reconcile held consumption."""
    import json
    from sqlalchemy import text
    from core.dependencies import log_audit
    from core.tray_identity import decode
    from modules.inventory.slot_assignments import lock_slot_graph
    printer = db.query(Printer).filter_by(id=printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise HTTPException(404, 'Printer not found')
    ensure_local_filament_management(printer_id)
    slot = db.query(FilamentSlot).filter_by(printer_id=printer_id, slot_number=slot_number).first()
    if slot is None:
        raise HTTPException(404, 'Slot not found')
    lock_slot_graph(db, slot.id, body.spool_id)
    db.refresh(slot)
    db.refresh(printer)
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(404, 'Printer not found')
    state = decode(slot.tray_observation)
    if state.get('id') != body.observed_state:
        raise HTTPException(409, 'Tray observation changed; refresh before confirming')
    prior = db.execute(text('SELECT decision,spool_id,previous_spool_id FROM tray_confirmations WHERE state_id=:state AND slot_id=:slot'),
                       dict(state=body.observed_state, slot=slot.id)).first()
    if prior:
        if prior[0] != body.decision or (body.spool_id is not None and prior[1] != body.spool_id):
            raise HTTPException(409, 'This observation already has a different confirmation')
        return dict(success=True, observed_state=body.observed_state, decision=prior[0], spool_id=prior[1], spool_change_pending=False, replayed=True)
    previous = slot.assigned_spool_id
    target = previous if body.decision == 'same' else body.spool_id
    if target is None:
        raise HTTPException(409, 'No bound spool; select a new spool')
    spool = db.query(Spool).filter_by(id=target).first()
    if not spool or not check_org_access(current_user, spool.org_id) or (spool.org_id is not None and spool.org_id != printer.org_id):
        raise HTTPException(404, 'Spool not found')
    if spool.status.value != 'active':
        raise HTTPException(409, 'Select an active spool')
    if body.decision == 'new' and target == previous:
        raise HTTPException(409, 'Select a different spool or confirm Same spool')
    assign_slot(db, slot, target, current_user)
    slot.spool_confirmed = True
    hold_id = state.get('hold_id', body.observed_state)
    state['pending'] = False
    state['confirmation_id'] = hold_id
    slot.tray_observation = json.dumps(state)
    for state_id in sorted({body.observed_state, hold_id}):
        db.execute(text('INSERT INTO tray_confirmations(state_id,slot_id,decision,spool_id,previous_spool_id) VALUES(:state,:slot,:decision,:spool,:previous)'),
                   dict(state=state_id, slot=slot.id, decision=body.decision, spool=target, previous=previous))
    db.flush()
    from core.interfaces.attempt_accounting import attempt_accounting
    archives = db.execute(text('''SELECT a.id,a.print_job_id,a.job_id,a.status,a.consumption_json,p.spool_snapshot,p.filament_used_g
        FROM print_archives a JOIN print_jobs p ON p.id=a.print_job_id
        WHERE a.printer_id=:printer AND a.ownership_state='known' AND a.accounting_state='unresolved'
        AND a.accounting_reason='spool_change_pending' ORDER BY a.id'''), dict(printer=printer_id)).all()
    reconciled = []
    for archive in archives:
        entries = json.loads(archive[4] or '[]')
        if not any(item.get('identity_state') in (body.observed_state, hold_id) for item in entries):
            continue
        outcome, reason, _ = attempt_accounting().account_archive(db.connection(), archive_id=archive[0], print_job_id=archive[1],
            printer_id=printer_id, snapshot_json=archive[5], scheduled_job_id=archive[2], measured_grams=archive[6], success=archive[3] == 'completed')
        db.execute(text('UPDATE print_archives SET accounting_state=:state,accounting_reason=:reason WHERE id=:id'),
                   dict(state=outcome, reason=reason, id=archive[0]))
        if outcome in ('accounted', 'reconciliation_required'):
            reconciled.append(archive[0])
    log_audit(db, 'tray_identity.confirmed', 'filament_slot', slot.id,
              dict(printer_id=printer_id, slot_number=slot_number, observed_state=body.observed_state,
                   observed=state['observed'], decision=body.decision, previous_spool_id=previous,
                   spool_id=target, reconciled_archive_ids=reconciled))
    db.commit()
    return dict(success=True, observed_state=body.observed_state, decision=body.decision,
                spool_id=target, spool_change_pending=False, replayed=False)
