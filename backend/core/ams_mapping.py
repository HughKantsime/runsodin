"""Shared explicit AMS validation and read-only filament suggestions."""
import json
from fastapi import HTTPException
from sqlalchemy import text
from core.print_payload import validate_mapping, source_filaments
from core.rbac import check_org_access


def accessible_printer(db, printer_id, actor):
    if printer_id is None:
        raise HTTPException(400, 'AMS mapping requires a specific target printer')
    printer = db.execute(text('SELECT id,api_type,org_id,shared,model,machine_type FROM printers WHERE id=:id'),
                         {'id': printer_id}).first()
    if not printer or not (check_org_access(actor, printer.org_id) or printer.shared):
        raise HTTPException(404, 'Printer not found')
    return printer


def validate_for_printer(db, mapping, filaments_json, printer_id, actor):
    if mapping is None:
        return None
    printer = accessible_printer(db, printer_id, actor)
    slots = dict(db.execute(text('SELECT slot_number,filament_type FROM filament_slots WHERE printer_id=:id'),
                            {'id': printer_id}).all())
    from modules.printers.dispatch_support import is_kobra_s1
    protocol = 'bambu' if is_kobra_s1(printer._mapping) else printer.api_type
    result = validate_mapping(mapping, filaments_json, slots, protocol)
    from modules.printers.slot_feed_policy import flagged_mapping_reason
    reason = flagged_mapping_reason(db, printer_id, result)
    if reason:
        raise HTTPException(400, reason)
    if is_kobra_s1(printer._mapping) and any(n >= 4 for n in result):
        raise HTTPException(400, 'S1 mapping requires ACE slots 1–4')
    return result


def preview(db, filaments_json, printer_id, actor):
    printer = accessible_printer(db, printer_id, actor)
    from modules.printers.dispatch_support import is_kobra_s1
    if printer.api_type != 'bambu' and not is_kobra_s1(printer._mapping):
        return None
    available = [dict(row) for row in db.execute(text(
        'SELECT slot_number AS slot,color,color_hex,filament_type AS material,assigned_spool_id AS spool_id,do_not_feed '
        'FROM filament_slots WHERE printer_id=:id ORDER BY slot_number'), {'id': printer_id}).mappings()]
    for slot in available:
        slot['do_not_feed'] = bool(slot['do_not_feed'])
    return preview_for_slots(filaments_json, available)


def preview_for_slots(filaments_json, available):
    """Same read-only suggestions for authorized API callers and dispatch."""
    from modules.archives.ams_mapping import suggest_mapping
    filaments = source_filaments(filaments_json)
    required = [{'slot': f['slot'], 'color': f.get('color', ''), 'material': f.get('type', '')}
                for f in filaments]
    return {'required': required, 'available': available, 'suggested_mapping': suggest_mapping(required, available)}


def mapping_audit_details(job, previous=None):
    """Details for the caller's atomic mapping audit and durable timeline event."""
    return {'ams_mapping': job.ams_mapping, 'previous_ams_mapping': previous}


def timeline_events(db, job_id):
    rows = db.execute(text("SELECT id,details,timestamp FROM audit_logs "
                           "WHERE entity_type='job' AND entity_id=:id AND action='job.ams_mapping_changed' "
                           "ORDER BY timestamp,id"), {'id': job_id}).mappings()
    return [{'id': f"audit:{row['id']}", 'print_attempt_uid': None, 'printer_id': None,
             'kind': 'ams_mapping_changed', 'code': None, 'detail': json.loads(row['details']),
             'occurred_at': row['timestamp'], 'recorded_at': row['timestamp']} for row in rows]
