"""A native restore marker, newer than its pre-restore anchor, is single-use authority."""
import json
from datetime import datetime
from sqlalchemy import text
from core.audit_receipts import authentic, seal


def pending_restore(connection, anchor):
    marker = connection.execute(text("SELECT id,chain_seq,row_hash,details FROM audit_logs "
        "WHERE action='restore_completed' ORDER BY chain_seq DESC LIMIT 1")).mappings().first()
    if marker is None:
        return None
    # Acceptance always consumes the latest marker. Both rows survive retention.
    if connection.execute(text("SELECT 1 FROM audit_logs WHERE action='audit.restore_baseline_accepted' "
                               'AND chain_seq>:seq LIMIT 1'), {'seq': marker['chain_seq']}).first():
        return None
    trusted = anchor if authentic(anchor) else None
    if trusted is None:
        # Without an authenticated time boundary, a stale restored marker is not
        # evidence that a restore just happened. Recover the original anchor.
        return None
    prior = trusted.get('pending_restore') or {}
    if (trusted.get('accepted_restore_baseline') or {}).get('restore_row_hash') == marker['row_hash']:
        return None
    floor = (prior.get('anchor_checked_at') if prior.get('row_hash') == marker['row_hash']
             else trusted['checked_at'])
    try:
        details = json.loads(marker['details']) if isinstance(marker['details'], str) else marker['details']
        if not authentic(details) or details.get('action') != 'restore_completed':
            return None
        restored_at = datetime.fromisoformat(details['restored_at'])
        if restored_at.tzinfo is None or restored_at.timestamp() <= floor:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return {'record_id': marker['id'], 'row_hash': marker['row_hash'], 'anchor_checked_at': floor}


def seal_marker(action, details, connection):
    """Only authenticated completion records can grant baseline acceptance."""
    if action != 'restore_completed':
        return details
    return seal({**details, 'action': action}, connection)
