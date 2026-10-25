"""Normalize historical free-text slots before any enum-backed ORM read."""
import json
from sqlalchemy import text
from core.base import FilamentType, slot_filament_value


def repair_slot_materials(connection):
    valid = {item.value for item in FilamentType}
    # Raw SQL deliberately bypasses the enum deserializer that broken rows trip.
    rows = connection.execute(text(
        'SELECT id,filament_type FROM filament_slots WHERE filament_type IS NOT NULL'
    )).all()
    for slot_id, before in rows:
        if before in valid:
            continue
        after = slot_filament_value(before)
        changed = connection.execute(text(
            'UPDATE filament_slots SET filament_type=:after WHERE id=:id AND filament_type=:before'
        ), dict(id=slot_id, before=before, after=after)).rowcount
        if changed:
            # Use the existing audit journal: repairs can include unassigned slots.
            connection.execute(text(
                'INSERT INTO audit_logs(action,entity_type,entity_id,details) '
                'VALUES(:action,:entity,:id,:details)'
            ), dict(action='slot-material.normalize', entity='filament_slot', id=slot_id,
                    details=json.dumps({'before': before, 'after': after})))
