"""Erasure of controlled policy audit authors, with authenticated E1 receipts."""
import json
from sqlalchemy import text
from core.audit_erasure import rewrite


def erased_details(details, user_id, *, actor_id=None):
    result = dict(details)
    for side in ('before', 'after'):
        value = result.get(side)
        if not isinstance(value, dict):
            continue
        if value.get('changed_by') == user_id or (side == 'after' and actor_id == user_id):
            result[side] = {**value, 'reason': '[deleted]'}
            if 'changed_by' in value:
                result[side]['changed_by'] = None
    return result


def erase_mark_history(db, user_id):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed action filter, no user input in SQL
    rows = db.execute(text("SELECT id,actor_user_id,details FROM audit_logs WHERE action='controlled_part.changed'"))
    for row in rows:
        try:
            before = json.loads(row.details) if isinstance(row.details, str) else row.details
        except (TypeError, ValueError):
            continue
        if not isinstance(before, dict):
            continue
        after = erased_details(before, user_id, actor_id=row.actor_user_id)
        if after != before:
            rewrite(db, 'UPDATE audit_logs SET details=:d WHERE id=:id',
                    {'id': row.id, 'd': json.dumps(after)})
