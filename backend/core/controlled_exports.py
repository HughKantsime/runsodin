"""Explicit acknowledgement for privileged exports containing restricted data."""
from core.controlled_parts import any_read_restrictions, refuse
from core.audit import audit_download


def acknowledge_export(db, actor, acknowledged, kind, *, record=True):
    marked = any_read_restrictions(db)
    if marked and acknowledged is not True:
        refuse(db, 'controlled_part_acknowledgement_required', 'system', None)
    if marked and (not actor or actor.get('role') != 'admin' or actor.get('group_id') is not None):
        refuse(db, 'controlled_part_superadmin_required', 'system', None)
    if marked and record:
        audit_download(db, 'controlled_part.privileged_export', 'system', None,
                       {'kind': kind, 'acknowledged': acknowledged is True})
    return marked
