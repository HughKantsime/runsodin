"""Durable, organization-scoped opened-roll checklist and sealed-stock correction."""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from core.audit import log_audit
from core.base import SpoolStatus
from core.db import get_db
from core.models import AuditLog
from core.rbac import require_role, require_action, check_org_access, get_org_scope, should_filter_org, _get_org_filter
from core.material_identity import canonical_reported_material
from modules.inventory.models import Spool
from modules.inventory import rfid_identity as rfid
from modules.inventory.slot_assignments import lock_slot_graph, _involved_graph
from modules.inventory.stock import break_seal
from modules.printers.models import Printer, FilamentSlot
from core.interfaces.filament_ownership import ensure_local_filament_management

router = APIRouter(prefix='/spools', tags=['Spools'])


def eligible_candidates(db, spool, details):
    ids = details.get('possible_duplicate_spool_ids', []) if details.get('possible_duplicate') else []
    filament = spool.filament
    if not filament or filament.brand.casefold() != 'bambu lab':
        return []
    return [row[0] for row in rfid.candidate_rows(db, spool.org_id)
            if row[0] in ids and row[0] != spool.id and row[2] == filament.name
            and row[4].casefold() == 'bambu lab'
            and canonical_reported_material(row[3]) == canonical_reported_material(filament.material)]


@router.get('/rfid-onboarding')
def rfid_onboarding(org_id: int | None = None,
                    current_user=Depends(require_role('viewer', scope='read:spools')),
                    db: Session = Depends(get_db)):
    org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    query = db.query(AuditLog, Spool).join(Spool, Spool.id == AuditLog.entity_id).filter(
        AuditLog.entity_type == 'spool', Spool.status != SpoolStatus.ARCHIVED,
        AuditLog.action.in_(['inventory.rfid_bound', 'inventory.rfid_created', 'inventory.rfid_identified', 'inventory.rfid_merged']))
    if should_filter_org(current_user, org):
        query = query.filter(Spool.org_id == org)
    result = {}
    for event, spool in query.order_by(AuditLog.id.desc()).all():
        if spool.id in result:
            continue
        details = event.details or {}
        result[spool.id] = dict(spool_id=spool.id, tray_uuid=spool.tray_uuid,
            color_name=spool.color_name, filament_name=spool.filament.name,
            outcome=event.action.removeprefix('inventory.rfid_'),
            printer_id=details.get('printer_id'), slot=details.get('slot'), found_at=event.timestamp,
            possible_duplicate=bool(details.get('possible_duplicate')), checked=True,
            possible_duplicate_spool_ids=eligible_candidates(db, spool, details))
    return list(reversed(result.values()))


class MergeRoll(BaseModel):
    sealed_spool_id: int = Field(gt=0)


def reference_reason(db, spool):
    # Explicit scalar references, including raw tables outside ORM metadata.
    references = db.execute(text('''
        SELECT 'job usage or deductions' AS reason FROM spool_usage WHERE spool_id=:id
        UNION ALL SELECT 'drying history' FROM drying_logs WHERE spool_id=:id
        UNION ALL SELECT 'alerts' FROM alerts WHERE spool_id=:id
        UNION ALL SELECT 'queued notifications' FROM quiet_hours_deferred_deliveries WHERE spool_id=:id
        UNION ALL SELECT 'pending consumption delivery' FROM filament_delivery_outbox WHERE spool_id=:id
        UNION ALL SELECT 'inventory repairs' FROM inventory_repairs WHERE spool_id=:id
        UNION ALL SELECT 'costing history' FROM filament_cost_events
            WHERE spool_uid=(SELECT economic_uid FROM spools WHERE id=:id)
        UNION ALL SELECT 'purchase evidence' FROM spool_purchase_evidence
            WHERE spool_uid=(SELECT economic_uid FROM spools WHERE id=:id)
        LIMIT 1'''), {'id': spool.id}).first()
    if references:
        return references[0]
    # A captured running/finished print is a reference even before deduction.
    uid = db.execute(text('SELECT economic_uid FROM spools WHERE id=:id'), {'id': spool.id}).scalar_one()
    def mentions(value):
        if isinstance(value, dict):
            if str(value.get('spool_id', '')) == str(spool.id) or (uid and value.get('spool_uid') == uid):
                return True
            return any(mentions(child) for child in value.values())
        return isinstance(value, list) and any(mentions(child) for child in value)
    for row in db.execute(text('SELECT spool_snapshot FROM print_jobs WHERE spool_snapshot IS NOT NULL')).yield_per(100):
        try:
            snapshot = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (TypeError, ValueError):
            return 'an unreadable print snapshot; resolve it before merging'
        if mentions(snapshot):
            return 'a captured print reference'
    if spool.spoolman_spool_id is not None:
        return 'Spoolman link'
    event = db.query(AuditLog.id).filter(AuditLog.entity_type == 'spool', AuditLog.entity_id == spool.id,
        ~AuditLog.action.in_(['inventory.rfid_created', 'inventory.rfid_identified'])).first()
    return 'other inventory history' if event else None


@router.post('/rfid-onboarding/{spool_id}/merge', dependencies=[Depends(require_role('operator', scope='write:spools'))])
def merge_rfid_roll(spool_id: int, body: MergeRoll,
                    current_user=Depends(require_action('spools.edit', scope='write:spools')),
                    db: Session = Depends(get_db)):
    try:
        return _merge(db, current_user, spool_id, body.sealed_spool_id)
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, 'sqlstate', None) == '55P03' or getattr(exc.orig, 'sqlite_errorcode', None) in (5, 6, 517):
            raise HTTPException(409, 'Spool assignments are busy; retry') from None
        raise
    except Exception:
        db.rollback()
        raise


def _merge(db, user, source_id, target_id):
    lock_slot_graph(db)  # SQLite writer boundary before reading; PostgreSQL graph below.
    source, target = db.get(Spool, source_id), db.get(Spool, target_id)
    if not source or not target or not all(check_org_access(user, s.org_id) for s in (source, target)):
        raise HTTPException(404, 'Spool not found')
    before = (_involved_graph(db, spool_id=source_id), _involved_graph(db, spool_id=target_id))
    printer_ids = sorted({p for graph in before for p in graph[0]})
    slot_ids = sorted({s[0] for graph in before for s in graph[1]})
    # Same global lock order as AMS: printers, slots, spools, each in id order.
    for model, ids in ((Printer, printer_ids), (FilamentSlot, slot_ids), (Spool, sorted({source_id, target_id}))):
        db.query(model).filter(model.id.in_(ids)).order_by(model.id).populate_existing().with_for_update(nowait=True).all()
    if before != (_involved_graph(db, spool_id=source_id), _involved_graph(db, spool_id=target_id)):
        raise HTTPException(409, 'Spool assignments changed; retry')
    if source.org_id != target.org_id or not all(check_org_access(user, s.org_id) for s in (source, target)):
        raise HTTPException(409, 'Spools must belong to the same organization')
    for printer in db.query(Printer).filter(Printer.id.in_(printer_ids)).all():
        if printer.org_id != source.org_id or not check_org_access(user, printer.org_id):
            raise HTTPException(409, 'Printer organization differs')
        ensure_local_filament_management(printer.id)
    event = db.query(AuditLog).filter_by(entity_type='spool', entity_id=source_id,
        action='inventory.rfid_created').order_by(AuditLog.id).first()
    if source.status == SpoolStatus.ARCHIVED or not (source.tray_uuid or source.rfid_tag) or not event:
        raise HTTPException(409, 'Source is no longer an auto-created RFID spool')
    if target_id not in eligible_candidates(db, source, event.details or {}):
        raise HTTPException(409, 'Sealed candidate is no longer eligible for this line and material')
    reason = reference_reason(db, source)
    if reason:
        raise HTTPException(409, 'Cannot merge: source has ' + reason)
    slots = db.query(FilamentSlot).filter_by(assigned_spool_id=source_id).all()
    if len(slots) > 1 or (slots and (source.location_printer_id, source.location_slot) != (slots[0].printer_id, slots[0].slot_number)):
        raise HTTPException(409, 'Source spool assignment is inconsistent')
    if not slots and (source.location_printer_id is not None or source.location_slot is not None):
        raise HTTPException(409, 'Source spool assignment is inconsistent')
    fields = ('tray_uuid', 'rfid_tag', 'color_hex', 'location_printer_id', 'location_slot',
              'storage_location')
    if source.remaining_measured_at is not None:
        fields += ('remaining_weight_g', 'remaining_measured_at',
                   'remaining_measured_g', 'remaining_measured_printer_id')
    values = {key: getattr(source, key) for key in fields}
    source.tray_uuid = source.rfid_tag = None
    source.location_printer_id = source.location_slot = source.storage_location = None
    source.remaining_measured_at = source.remaining_measured_g = source.remaining_measured_printer_id = None
    source.status = SpoolStatus.ARCHIVED
    db.flush()  # Release unique RFID identity before moving it to the sealed record.
    for key, value in values.items():
        setattr(target, key, value)
    for slot in slots:
        slot.assigned_spool_id = target_id
    break_seal(db, target_id)
    log_audit(db, 'inventory.rfid_merged', 'spool', target_id,
              dict(spool_id=target_id, source_spool_id=source_id, tray_uuid=target.tray_uuid,
                   printer_id=target.location_printer_id, slot=target.location_slot))
    db.commit()
    return dict(spool_id=target_id, archived_spool_id=source_id)
