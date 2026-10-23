"""Transactional spool moves shared by API writers (caller owns commit)."""
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError
from core.rbac import check_org_access
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot, Printer


def _involved_graph(db, slot_id=None, spool_id=None):
    """Snapshot only associations that can be changed by this move."""
    slots = db.query(FilamentSlot.id, FilamentSlot.printer_id, FilamentSlot.slot_number, FilamentSlot.assigned_spool_id).all()
    spools = db.query(Spool.id, Spool.location_printer_id, Spool.location_slot).all()
    target = next((s for s in slots if s.id == slot_id), None)
    involved_slots = [s for s in slots if s.id == slot_id or
                      (spool_id is not None and s.assigned_spool_id == spool_id)]
    spool_ids = {spool_id} - {None}
    if target:
        spool_ids.update(s.id for s in spools if
                         (s.location_printer_id, s.location_slot) ==
                         (target.printer_id, target.slot_number))
        if target.assigned_spool_id is not None:
            spool_ids.add(target.assigned_spool_id)
    involved_spools = [s for s in spools if s.id in spool_ids]
    printers = {s.printer_id for s in involved_slots}
    printers.update(s.location_printer_id for s in involved_spools
                    if s.location_printer_id is not None)
    return (tuple(sorted(printers)),
            tuple(sorted((s.id, s.assigned_spool_id, s.printer_id, s.slot_number)
                         for s in involved_slots)),
            tuple(sorted((s.id, s.location_printer_id, s.location_slot)
                         for s in involved_spools)))


def lock_slot_graph(db, slot_id=None, spool_id=None):
    """Printers, then slots, then spools, sorted, like the AMS monitor.

    A no-target call establishes only SQLite's writer boundary. PostgreSQL
    callers acquire the exact move graph before changing any associations.
    Recheck after locking so a concurrent graph expansion fails closed.
    """
    try:
        with db.no_autoflush:
            connection = db.connection()
            if connection.dialect.name == 'sqlite':
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                else:
                    connection.exec_driver_sql('UPDATE printers SET name=name WHERE 1=0')
            elif connection.dialect.name == 'postgresql':
                if slot_id is None and spool_id is None:
                    return
                before = _involved_graph(db, slot_id, spool_id)
                for model, ids in ((Printer, before[0]),
                                   (FilamentSlot, [s[0] for s in before[1]]),
                                   (Spool, [s[0] for s in before[2]])):
                    if ids:
                        db.query(model.id).filter(model.id.in_(ids)).order_by(
                            model.id).with_for_update(nowait=True).all()
                if _involved_graph(db, slot_id, spool_id) != before:
                    raise HTTPException(409, 'Spool assignments changed; retry')
            else:
                raise HTTPException(500, 'Unsupported database for slot assignments')
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, 'sqlstate', None) == '55P03' or getattr(exc.orig, 'sqlite_errorcode', None) in (5, 6, 517):
            raise HTTPException(409, 'Spool assignments are busy; retry') from None
        raise


def assign_slot(db, slot, spool_id, current_user=None):
    """Move a spool, or unassign a slot, preserving both graph directions."""
    lock_slot_graph(db, slot.id, spool_id)
    db.flush()
    with db.no_autoflush:
        # Flush above preserves metadata pending from earlier moves in an AMS
        # batch. Re-read associations after acquiring the writer boundary so
        # pre-lock ORM snapshots cannot resurrect a concurrent assignment.
        spools = db.query(Spool).populate_existing().all()
        slots = db.query(FilamentSlot).populate_existing().all()
        target = next((row for row in spools if row.id == spool_id), None)
        if spool_id is not None and target is None:
            raise HTTPException(404, 'Spool not found')
        affected = [row for row in slots if spool_id is not None and row.assigned_spool_id == spool_id]
        displaced = [row for row in spools if (row.location_printer_id, row.location_slot) == (slot.printer_id, slot.slot_number)]
        if current_user is not None:
            previous = [row for row in spools if row.id == slot.assigned_spool_id]
            for row in displaced + previous + ([target] if target else []):
                if not check_org_access(current_user, row.org_id):
                    raise HTTPException(404, 'Spool not found')
            printer_ids = {row.printer_id for row in affected} | {slot.printer_id}
            if target and target.location_printer_id is not None:
                printer_ids.add(target.location_printer_id)
            for printer_id in printer_ids:
                printer = db.query(Printer).filter_by(id=printer_id).populate_existing().first()
                if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
                    raise HTTPException(404, 'Printer not found')
        for row in displaced:
            row.location_printer_id = None
            row.location_slot = None
        for row in affected:
            if row.id != slot.id:
                row.assigned_spool_id = None
                row.spool_confirmed = False
        # Release old unique links before setting the destination (ORM update order is by id).
        db.flush()
        slot.assigned_spool_id = spool_id
        if target:
            target.location_printer_id = slot.printer_id
            target.location_slot = slot.slot_number
            target.storage_location = None
        else:
            slot.spool_confirmed = False


def unload_spool_graph(db, spool, current_user=None):
    lock_slot_graph(db, spool_id=spool.id)
    db.refresh(spool, ['location_printer_id', 'location_slot'])
    slots = db.query(FilamentSlot).filter(FilamentSlot.assigned_spool_id == spool.id).populate_existing().all()
    if current_user is not None:
        for printer_id in {slot.printer_id for slot in slots}:
            printer = db.query(Printer).filter_by(id=printer_id).populate_existing().first()
            if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
                raise HTTPException(404, 'Printer not found')
    for slot in slots:
        slot.assigned_spool_id = None
        slot.spool_confirmed = False
    spool.location_printer_id = None
    spool.location_slot = None


def slot_integrity_report(db):
    """Read only; include duplicate links and contradictions in either direction."""
    with db.no_autoflush:
        slots = db.query(FilamentSlot.id, FilamentSlot.printer_id, FilamentSlot.slot_number, FilamentSlot.assigned_spool_id).all()
        spools = {row.id: row for row in db.query(Spool.id, Spool.location_printer_id, Spool.location_slot)}
    links = {}
    contradictions = []
    by_location = {(row.printer_id, row.slot_number): row for row in slots}
    for slot in slots:
        if slot.assigned_spool_id is None:
            continue
        links.setdefault(slot.assigned_spool_id, []).append(slot.id)
        spool = spools.get(slot.assigned_spool_id)
        if spool is None or (spool.location_printer_id, spool.location_slot) != (slot.printer_id, slot.slot_number):
            contradictions.append(dict(spool_id=slot.assigned_spool_id, filament_slot_id=slot.id, direction='slot'))
    for spool in spools.values():
        if spool.location_printer_id is None and spool.location_slot is None:
            continue
        slot = by_location.get((spool.location_printer_id, spool.location_slot))
        if slot is None or slot.assigned_spool_id != spool.id:
            contradictions.append(dict(spool_id=spool.id, filament_slot_id=slot.id if slot else None, direction='location'))
    return dict(duplicates=[dict(spool_id=key, filament_slot_ids=sorted(value)) for key, value in sorted(links.items()) if len(value) > 1], contradictions=contradictions)
