"""Repair only unlinked spool locations after the caller locks the move graph."""
import json
import uuid
from sqlalchemy import text
from modules.printers.models import FilamentSlot


def repair_stale_locations(db, spools, actor_id):
    for spool in spools:
        if spool.location_printer_id is None and spool.location_slot is None:
            continue
        if db.query(FilamentSlot.id).filter_by(assigned_spool_id=spool.id).first():
            continue
        before = [spool.location_printer_id, spool.location_slot]
        db.execute(text('INSERT INTO inventory_repairs(event_id,spool_id,actor_id,action,before_json,after_json) '
            'VALUES(:event,:spool,:actor,:action,:before,:after)'),
            dict(event=str(uuid.uuid4()), spool=spool.id, actor=actor_id, action='stale-location.clear',
                 before=json.dumps(before), after=json.dumps([None, None])))
        spool.location_printer_id = None
        spool.location_slot = None
