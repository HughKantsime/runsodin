"""Read-only catalogue labels from existing slot facts; no printer commands."""
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import set_committed_value

from core.display_json import display_object, display_tray_state
from core.material_identity import canonical_reported_material
from core.rbac import check_org_access
from modules.inventory.models import FilamentLibrary, Spool
from modules.printers.models import FilamentSlot


def reported_slot(slot):
    state = display_tray_state(slot.tray_observation)
    if slot.tray_observation:
        observed = state.get('observed')
        if not isinstance(observed, dict) or observed.get('exists') is False:
            return None, None
        return observed.get('type'), observed.get('color')
    printer = slot.printer
    if printer and (printer.ace_status or printer.api_type == 'anycubic_lan'):
        ace = display_object(printer.ace_status)
        slots = ace.get('slots') if ace else None
        if not isinstance(slots, list) or any(not isinstance(fact, dict) for fact in slots):
            return None, None
        facts = [fact for fact in slots if fact.get('index') == slot.slot_number - 1]
        if len(facts) != 1 or facts[0].get('status') == 'empty':
            return None, None
        return facts[0].get('type'), facts[0].get('color_hex')
    return getattr(slot.filament_type, 'value', slot.filament_type), slot.color_hex


def _key(material, color):
    import re
    material = canonical_reported_material(material)
    if not isinstance(color, str):
        return None
    color = color.lstrip('#').upper()
    if material is None or not re.fullmatch('[0-9A-F]{6}([0-9A-F]{2})?', color):
        return None
    return material, color[:6]


def _labels(slot, actor, candidates):
    selected = None
    spool = slot.assigned_spool
    if spool and check_org_access(actor, spool.org_id) and not display_tray_state(slot.tray_observation).get('pending'):
        selected = next((f for f in candidates if f.id == spool.filament_id), None)
    if selected is None and len(candidates) == 1:
        selected = candidates[0]
    return dict(catalogue_name=selected.name if selected else None,
                catalogue_choices=[dict(id=f.id, name=f.name) for f in candidates]
                if not selected and len(candidates) > 1 else [])


def slot_catalogue(db, slot, actor):
    from modules.inventory.catalogue import matching_entries
    material, color = reported_slot(slot)
    return _labels(slot, actor, matching_entries(db, material, color))


def enrich_catalogue_slots(db, printers, actor):
    """Two queries for the entire response, including spool/filament relationships."""
    if not printers:
        return printers
    slots = db.query(FilamentSlot).filter(FilamentSlot.printer_id.in_([p.id for p in printers])).options(
        joinedload(FilamentSlot.assigned_spool).joinedload(Spool.filament)).order_by(FilamentSlot.slot_number).all()
    entries = db.query(FilamentLibrary).filter(FilamentLibrary.catalogue_code.isnot(None)).order_by(FilamentLibrary.id).all()
    by_key, by_printer = defaultdict(list), defaultdict(list)
    for entry in entries:
        key = _key(entry.material, entry.color_hex)
        if key:
            by_key[key].append(entry)
    parents = {p.id: p for p in printers}
    for slot in slots:
        set_committed_value(slot, 'printer', parents[slot.printer_id])
        labels = _labels(slot, actor, by_key.get(_key(*reported_slot(slot)), []))
        slot.catalogue_name = labels['catalogue_name']
        slot.catalogue_choices = labels['catalogue_choices']
        by_printer[slot.printer_id].append(slot)
    for printer in printers:
        set_committed_value(printer, 'filament_slots', by_printer[printer.id])
    return printers


def select_spool_filament(db, spool, filament_id, actor):
    """Store the choice on the spool, preserving any measured spool colour."""
    if not check_org_access(actor, spool.org_id):
        raise HTTPException(404, 'Spool not found')
    filament = db.query(FilamentLibrary).filter_by(id=filament_id).first()
    if filament is None:
        raise HTTPException(404, 'Filament not found')
    if spool.filament_id == filament.id:
        return filament
    # A spool with no library filament has no material to keep: refused below, never a crash.
    material = canonical_reported_material(spool.filament.material if spool.filament else None)
    slots = db.query(FilamentSlot).filter(FilamentSlot.assigned_spool_id == spool.id).populate_existing().all()
    for slot in slots:
        if canonical_reported_material(reported_slot(slot)[0]) != material:
            raise HTTPException(409, 'The spool and the slot disagree about the material. Confirm the spool before choosing its catalogue name.')
    if (filament.catalogue_code is None or material is None
            or canonical_reported_material(filament.material) != material):
        raise HTTPException(409, 'Choose a catalogue filament with the same material as this spool.')
    spool.filament_id = filament.id
    return filament
