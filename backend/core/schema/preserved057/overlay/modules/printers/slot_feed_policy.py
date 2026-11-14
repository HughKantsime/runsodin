"""Slot feed exclusions shared by all dispatch protocols."""
import json
from sqlalchemy import text
from fastapi import HTTPException

POLICY_FIELDS = frozenset({'do_not_feed', 'do_not_feed_note'})


def flagged_mapping_reason(conn, printer_id, mapping):
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except ValueError:
            return 'ams_mapping_invalid: Stored AMS mapping is invalid'
    if mapping is None:
        return None
    if type(mapping) is not list or any(type(n) is not int for n in mapping):
        return 'ams_mapping_invalid: Stored AMS mapping is invalid'
    targets = {n + 1 for n in mapping if n >= 0}
    rows = conn.execute(text('SELECT slot_number,do_not_feed_note FROM filament_slots '
                             'WHERE printer_id=:id AND do_not_feed IS TRUE ORDER BY slot_number'),
                        {'id': printer_id}).all()
    for number, note in rows:
        if number in targets:
            return f'slot_do_not_feed: Slot {number}: {note or "Do not feed"}'
    return None


def _lock_printer(db, printer_id):
    """The one lock final admission and every feed-policy writer serialize on."""
    from modules.printers.models import Printer
    if db.get_bind().dialect.name == 'postgresql':
        db.query(Printer).filter_by(id=printer_id).with_for_update().one()
    else:
        db.execute(text('UPDATE printers SET name=name WHERE id=:id'), {'id': printer_id})


def slots_removable_by_count(db, printer_id, new_count, first_kept):
    """Slots a count reduction may delete. A flag committed meanwhile refuses it, never deleted."""
    from modules.printers.models import FilamentSlot
    _lock_printer(db, printer_id)
    locked = db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).populate_existing()
    if db.get_bind().dialect.name == 'postgresql':
        locked = locked.with_for_update()
    removed = [s for s in locked.all() if new_count < s.slot_number < first_kept]
    restricted = sorted(s.slot_number for s in removed if s.do_not_feed)
    if restricted:
        raise HTTPException(409, f"Slot {restricted[0]} is marked do not feed; an admin must clear it before the slot count is reduced")
    return removed


def refuse_delete_while_flagged(db, printer_id):
    """A restriction is removed only by an audited admin clear, never by deleting the printer."""
    _lock_printer(db, printer_id)
    restricted = db.execute(text('SELECT slot_number FROM filament_slots WHERE printer_id=:id AND do_not_feed IS TRUE '
                                 'ORDER BY slot_number'), {'id': printer_id}).scalars().first()
    if restricted is not None:
        raise HTTPException(409, f"Slot {restricted} is marked do not feed; an admin must clear it before the printer is deleted")


def policy_change(db, slot, values, actor):
    """Serialize against final admission, then return the audit details of a real change.

    The caller stages the audit and commits it with the slot update.
    """
    if not POLICY_FIELDS.intersection(values):
        return None
    if actor.get('role') != 'admin':
        raise HTTPException(403, 'Only admins may change the do not feed flag or note')
    _lock_printer(db, slot.printer_id)
    if db.get_bind().dialect.name == 'postgresql':
        db.refresh(slot, with_for_update=True)
    else:
        db.refresh(slot)
    before = dict(do_not_feed=bool(slot.do_not_feed), note=slot.do_not_feed_note)
    after = dict(do_not_feed=values.get('do_not_feed', before['do_not_feed']),
                 note=values.get('do_not_feed_note', before['note']))
    if before == after:
        return None
    return dict(printer_id=slot.printer_id, slot_number=slot.slot_number,
                before=before, after=after, note=after['note'])
