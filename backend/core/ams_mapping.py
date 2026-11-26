"""Shared explicit AMS validation and read-only filament suggestions."""
import json
import logging
import os
from dataclasses import asdict
from functools import lru_cache
from fastapi import HTTPException
from sqlalchemy import text
from core.print_payload import validate_mapping, source_filaments
from core.print_payload_snapshot import PayloadUnavailable
from core.rbac import check_org_access

log = logging.getLogger(__name__)


def accessible_printer(db, printer_id, actor):
    if printer_id is None:
        raise HTTPException(400, 'AMS mapping requires a specific target printer')
    printer = db.execute(text('SELECT id,api_type,org_id,shared,model,machine_type FROM printers WHERE id=:id'),
                         {'id': printer_id}).first()
    if not printer or not (check_org_access(actor, printer.org_id) or printer.shared):
        raise HTTPException(404, 'Printer not found')
    return printer


def s1_filaments_json(filaments_json, stored_path):
    """A Kobra S1 mapping needs the file's filaments, including for a gcode row stored as []."""
    from modules.printers.kobra_s1 import stored_or_file_materials
    return json.dumps(stored_or_file_materials(filaments_json, stored_path))


def validate_for_printer(db, mapping, filaments_json, printer_id, actor, *, stored_path=None, plate_index=0, check_material=True):
    if mapping is None:
        return None
    printer = accessible_printer(db, printer_id, actor)
    from modules.printers.dispatch_support import is_kobra_s1
    protocol = 'bambu' if is_kobra_s1(printer._mapping) else printer.api_type
    if is_kobra_s1(printer._mapping):
        from modules.printers.kobra_s1 import S1Refusal
        try:
            filaments_json = s1_filaments_json(filaments_json, stored_path)
            if isinstance(mapping, list) and mapping and all(type(n) is int for n in mapping):
                if len(mapping) != len(source_filaments(filaments_json)):
                    raise HTTPException(400, 'AMS mapping must cover every source filament')
                mapping = [{'source_slot': i + 1, 'target_slot': slot + 1} for i, slot in enumerate(mapping)]
        except S1Refusal as refusal:
            raise HTTPException(400, str(refusal)) from None
    slots = mapping_materials(db, printer_id, printer.api_type)
    from modules.printers.services import is_h3c_printer
    h3c = is_h3c_printer(printer._mapping)
    if h3c:
        from modules.printers.services import rack_tool_refusal, rack_material_refusal
        from core.print_payload import MappingMaterialMismatch, MappingSlotUnavailable
        try:
            result = validate_mapping(mapping, filaments_json, slots, protocol, check_material=False)
        except MappingSlotUnavailable as exc:
            if exc.target_slot > 9:
                raise HTTPException(400, 'Choose an onboard tray in positions 1 to 9.') from None
            raise
        sources = read_rack_sources(stored_path, plate_index)
        if not sources or {s['source_index'] for s in sources} != {i for i, n in enumerate(result) if n >= 0}:
            raise HTTPException(400, 'This mapping needs a verified H3C rack slice.')
        from modules.printers.models import Printer
        detail = rack_tool_refusal(sources, result, db.get(Printer, printer_id).installed_nozzles)
        if detail:
            raise HTTPException(400, detail)
        if check_material:
            for source in sources:
                position = result[source['source_index']] + 1
                detail = rack_material_refusal(source, position, slots.get(position))
                if detail:
                    refusal = MappingMaterialMismatch(position)
                    refusal.detail = detail
                    raise refusal
    result = validate_mapping(mapping, filaments_json, slots, protocol, check_material=check_material)
    from modules.printers.slot_feed_policy import flagged_mapping_reason
    reason = flagged_mapping_reason(db, printer_id, result)
    if reason:
        raise HTTPException(400, reason)
    if is_kobra_s1(printer._mapping):
        from modules.printers.services import is_ace_slot
        if any(not is_ace_slot(n + 1) for n in result if n != -1):
            raise HTTPException(400, 'S1 mapping requires ACE slots 1–4')
        used = [n for n in result if n != -1]
        if len(set(used)) != len(used):
            raise HTTPException(400, 'S1 mapping needs a separate ACE slot for each file filament')
    return result


def _ht_material(stored, reported):
    """The exact reported label is usable only while it agrees with the inventory slot."""
    from core.base import slot_filament_value
    return reported if reported and slot_filament_value(reported) == stored else None


def mapping_materials(db, printer_id, api_type):
    slots = dict(db.execute(text('SELECT slot_number,filament_type FROM filament_slots WHERE printer_id=:id'),
                            {'id': printer_id}).all())
    if any(n >= 17 for n in slots):
        from core.printer_display_facts import reported_ht_slots
        # The inventory enum collapses some nylon and support materials.
        # Compare the exact reported label for HT targets, including unknowns.
        reported = reported_ht_slots(db, printer_id) if api_type == 'bambu' else {}
        slots = {n: material if n < 17 else _ht_material(material, reported[n]) for n, material in slots.items() if n < 17 or n in reported}
    return slots


def selectable_slots(db, printer_id, api_type, available):
    if api_type != 'bambu' or not any(s['slot'] >= 17 for s in available):
        return available
    from core.printer_display_facts import reported_ht_slots
    ht = reported_ht_slots(db, printer_id)
    return [s if s['slot'] < 17 else {**s, 'material': _ht_material(s['material'], ht[s['slot']]) or ''}
            for s in available if s['slot'] < 17 or s['slot'] in ht]


@lru_cache(maxsize=64)
def _rack_sources(path, identity, plate_index):
    """Cache only file assignments. Current tray contents are read on every preview."""
    from core.ams_source_inspection import inspect_ams_sources, VerifiedRackSource
    from core.print_payload_snapshot import _regular_source
    with _regular_source(path) as (stream, info):
        if (info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns) != identity:
            raise PayloadUnavailable('File changed before rack inspection')
        facts = inspect_ams_sources(stream, plate_index)
    if not facts.sources or not all(isinstance(source, VerifiedRackSource) for source in facts.sources):
        return ()
    return facts.sources


def read_rack_sources(path, plate_index):
    if path:
        try:
            info = os.stat(path, follow_symlinks=False)
            identity = info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns
            return [asdict(source) for source in _rack_sources(path, identity, plate_index)]
        except Exception as exc:
            log.warning('Could not read rack tools (%s)', type(exc).__name__)
    return []


def preview(db, filaments_json, printer_id, actor, *, stored_path=None, plate_index=0, stored_mapping=None):
    printer = accessible_printer(db, printer_id, actor)
    from modules.printers.dispatch_support import is_kobra_s1
    if printer.api_type != 'bambu' and not is_kobra_s1(printer._mapping):
        return None
    available = [dict(row) for row in db.execute(text(
        'SELECT slot_number AS slot,color,color_hex,filament_type AS material,assigned_spool_id AS spool_id,do_not_feed '
        'FROM filament_slots WHERE printer_id=:id ORDER BY slot_number'), {'id': printer_id}).mappings()]
    from modules.inventory.services import enrich_catalogue_slots
    from modules.printers.models import Printer
    labelled = enrich_catalogue_slots(db, [db.get(Printer, printer_id)], actor)[0]
    names = {slot.slot_number: slot.catalogue_name for slot in labelled.filament_slots}
    for slot in available:
        slot['catalogue_name'] = names.get(slot['slot'])
        slot['do_not_feed'] = bool(slot['do_not_feed'])
    if is_kobra_s1(printer._mapping):
        from modules.printers.kobra_s1 import S1Refusal
        from modules.printers.services import is_ace_slot
        available = [slot for slot in available if is_ace_slot(slot['slot'])]
        try:
            filaments_json = s1_filaments_json(filaments_json, stored_path)
        except S1Refusal as refusal:
            # Not a preview failure: a held job can still be created and started by hand.
            return {'required': [], 'available': available, 'suggested_mapping': [], 'file_note': refusal.detail}
        # Real S1 headers carry the slicer's default green, so a material match says nothing about the slot.
        return {**preview_for_slots(filaments_json, available), 'confirm_suggestions': True}
    from modules.printers.printer_models import identified_h3c
    if identified_h3c(printer._mapping):
        from modules.printers.services import rack_preview
        available = [tray for tray in available if 1 <= tray['slot'] <= 9]
        sources = read_rack_sources(stored_path, plate_index)
        if sources:
            return rack_preview(sources, available, db.get(Printer, printer_id).installed_nozzles, stored_mapping)
        return {**preview_for_slots(filaments_json, available), 'rack_mapping': [], 'suggested_mapping': [],
                'file_note': 'The rack tools could not be read from this file.'}
    return preview_for_slots(filaments_json, selectable_slots(db, printer_id, printer.api_type, available))


def preview_for_slots(filaments_json, available):
    """Same read-only suggestions for authorized API callers and dispatch."""
    from modules.archives.ams_mapping import suggest_mapping
    filaments = source_filaments(filaments_json)
    required = [{'slot': f['slot'], 'color': f.get('color', ''), 'material': f.get('type', '')}
                for f in filaments]
    return {'required': required, 'available': available, 'suggested_mapping': suggest_mapping(required, available)}


def mapping_audit_details(job, previous=None):
    """Details for the caller's atomic mapping audit and durable timeline event."""
    details = {'ams_mapping': job.ams_mapping, 'previous_ams_mapping': previous}
    if job.hold is True and job.ams_mapping is not None:
        details['message'] = 'mapping planned, not checked against the loaded trays'
    return details


def timeline_events(db, job_id):
    rows = db.execute(text("SELECT id,details,timestamp FROM audit_logs "
                           "WHERE entity_type='job' AND entity_id=:id AND action='job.ams_mapping_changed' "
                           "ORDER BY timestamp,id"), {'id': job_id}).mappings()
    return [{'id': f"audit:{row['id']}", 'print_attempt_uid': None, 'printer_id': None,
             'kind': 'ams_mapping_changed', 'code': None, 'detail': json.loads(row['details']),
             'occurred_at': row['timestamp'], 'recorded_at': row['timestamp']} for row in rows]
