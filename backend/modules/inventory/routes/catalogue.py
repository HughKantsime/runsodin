"""Local file import for the installation-wide filament library."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from fastapi.concurrency import run_in_threadpool
from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_superadmin
from modules.inventory.catalogue import MAX_BYTES, parse_catalogue, import_catalogue

catalogue_admin = require_superadmin(scope='write:spools')

router = APIRouter(prefix='/filaments', tags=['Filaments'])


@router.post('/import-bambu')
async def import_bambu_catalogue(request: Request,
    current_user: dict = Depends(catalogue_admin),
    db: Session = Depends(get_db)):
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise HTTPException(415, 'Choose a JSON catalogue file.')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, 'Choose a catalogue file smaller than 1 MB.')
        raw.extend(chunk)
    try:
        entries = parse_catalogue(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return await run_in_threadpool(_import_and_commit, db, entries)


def _import_and_commit(db, entries):
    result = import_catalogue(db, entries)
    log_audit(db, 'filament.catalogue_imported', 'filament_library', None,
              {key: value for key, value in result.items() if key != 'unrecognised_types'})
    db.commit()
    return result


from pydantic import BaseModel, Field
from core.rbac import require_action, check_org_access
from core.interfaces.filament_ownership import ensure_local_filament_management
from modules.printers.models import Printer, FilamentSlot
from modules.inventory.catalogue_slots import reported_slot, select_spool_filament, guard_catalogue_slots
from modules.inventory.catalogue import matching_entries


class CatalogueChoice(BaseModel):
    filament_id: int = Field(gt=0)
    spool_id: int = Field(gt=0)


# A separate router keeps this label-only operation out of the printer command paths.
slot_router = APIRouter(prefix='/printers', tags=['Filament Slots'])


@slot_router.post('/{printer_id}/slots/{slot_number}/catalogue')
def choose_slot_catalogue(printer_id: int, slot_number: int, body: CatalogueChoice,
    current_user: dict = Depends(require_action('spools.edit', scope='write:spools')),
    db: Session = Depends(get_db)):
    printer = db.query(Printer).filter_by(id=printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise HTTPException(404, 'Printer not found')
    ensure_local_filament_management(printer_id)
    slot = db.query(FilamentSlot).filter_by(printer_id=printer_id, slot_number=slot_number).first()
    if slot is None:
        raise HTTPException(404, 'Slot not found')
    guard_catalogue_slots(db, body.spool_id, slot)
    db.refresh(printer)
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(404, 'Printer not found')
    material, color = reported_slot(slot)
    if body.filament_id not in {f.id for f in matching_entries(db, material, color)}:
        raise HTTPException(409, 'This filament does not match the reported material and colour. Refresh the slot and try again.')
    spool = slot.assigned_spool
    changed = spool.filament_id != body.filament_id
    select_spool_filament(db, spool, body.filament_id, current_user)
    if changed:
        log_audit(db, 'spool.catalogue_selected', 'spool', body.spool_id, {'filament_id': body.filament_id})
    db.commit()
    return {'success': True}
