"""Tenant-bound explicit resolution of historical inventory contradictions."""
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from core.db import get_db
from core.rbac import require_role, check_org_access
from sqlalchemy import text
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot, Printer
from modules.inventory.slot_assignments import slot_integrity_report, assign_slot, unload_spool_graph, lock_slot_graph

router = APIRouter(prefix='/inventory/conflicts', tags=['Inventory'])


class Resolution(BaseModel):
    spool_id: int = Field(gt=0)
    slot_id: int | None = Field(default=None, gt=0)


def _visible(db, user, spool_id):
    spool = db.get(Spool, spool_id)
    if not spool or not check_org_access(user, spool.org_id):
        return None
    slots = db.query(FilamentSlot).filter_by(assigned_spool_id=spool_id).all()
    printers = {s.printer_id for s in slots}
    if spool.location_printer_id:
        printers.add(spool.location_printer_id)
    if any(not check_org_access(user, p.org_id) for p in db.query(Printer).filter(Printer.id.in_(printers))):
        return None
    return spool


@router.get('')
def conflicts(current_user=Depends(require_role('admin', scope='read:spools')), db=Depends(get_db)):
    report = slot_integrity_report(db)
    ids = {r['spool_id'] for rows in report.values() for r in rows}
    visible = {i for i in ids if _visible(db, current_user, i)}
    slots = db.query(FilamentSlot).join(Printer, Printer.id == FilamentSlot.printer_id).all()
    choices = [dict(id=s.id, printer_id=s.printer_id, slot_number=s.slot_number)
               for s in slots if check_org_access(current_user, db.get(Printer, s.printer_id).org_id)]
    return {**{k: [r for r in rows if r['spool_id'] in visible] for k, rows in report.items()}, 'slots': choices}


@router.post('/resolve')
def resolve(body: Resolution, current_user=Depends(require_role('admin', scope='write:spools')), db=Depends(get_db)):
    spool = _visible(db, current_user, body.spool_id)
    if not spool:
        raise HTTPException(404, 'Spool not found')
    lock_slot_graph(db, body.slot_id, body.spool_id)
    before = slot_integrity_report(db)
    if body.slot_id is None:
        unload_spool_graph(db, spool, current_user)
    else:
        slot = db.get(FilamentSlot, body.slot_id)
        if not slot or not check_org_access(current_user, db.get(Printer, slot.printer_id).org_id):
            raise HTTPException(404, 'Slot not found')
        printer = db.get(Printer, slot.printer_id)
        if printer.org_id is not None and spool.org_id is not None and printer.org_id != spool.org_id:
            raise HTTPException(409, 'Spool and true slot must belong to the same organisation')
        assign_slot(db, slot, spool.id, current_user)
    db.flush()
    db.execute(text('INSERT INTO inventory_repairs(event_id,spool_id,actor_id,action,before_json,after_json) '
                    'VALUES(:event,:spool,:actor,:action,:before,:after)'),
               dict(event=str(uuid.uuid4()), spool=spool.id, actor=current_user['id'], action='admin.resolve',
                    before=json.dumps(before), after=json.dumps(slot_integrity_report(db))))
    db.commit()
    return {'resolved': True}
