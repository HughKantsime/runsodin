"""Slot feed exclusions shared by all dispatch protocols."""
import json
from sqlalchemy import text
from fastapi import HTTPException


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


def update_policy(db, slot, values, actor):
    if not {'do_not_feed', 'do_not_feed_note'}.intersection(values):
        return
    if actor.get('role') != 'admin':
        raise HTTPException(403, 'Only admins may change the do not feed flag or note')
    from core.audit import log_audit
    before = dict(do_not_feed=bool(slot.do_not_feed), note=slot.do_not_feed_note)
    after = dict(do_not_feed=values.get('do_not_feed', before['do_not_feed']),
                 note=values.get('do_not_feed_note', before['note']))
    if before != after:
        log_audit(db, 'filament_slot.feed_policy', 'filament_slot', slot.id,
                  dict(printer_id=slot.printer_id, slot_number=slot.slot_number,
                       before=before, after=after, note=after['note']))
