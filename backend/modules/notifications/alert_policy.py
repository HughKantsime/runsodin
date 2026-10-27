"""Case-tolerant repeat suppression shared by API and daemons."""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text


def error_identity(message):
    message = message or ''
    prefix = re.match(r'^\[[^\]\s:]+:[^\]]+\]', message)
    return prefix.group(0) if prefix else message


def active_resource_duplicate(db, user_id, alert_type, printer_id, spool_id, message=''):
    """Unread/undismissed spool alerts persist; printer identities expire at 6h."""
    kind = getattr(alert_type, 'value', alert_type).lower()
    params = {'user': user_id, 'kind': kind}
    if kind == 'spool_low' and spool_id is not None:
        return db.execute(text(
            "SELECT 1 FROM alerts WHERE user_id=:user AND LOWER(alert_type)=:kind "
            "AND is_read IS FALSE AND is_dismissed IS FALSE AND spool_id=:spool LIMIT 1"),
            {**params, 'spool': spool_id}).first() is not None
    if kind == 'printer_error' and printer_id is not None:
        rows = db.execute(text(
            "SELECT message FROM alerts WHERE user_id=:user AND LOWER(alert_type)=:kind "
            "AND is_read IS FALSE AND is_dismissed IS FALSE "
            "AND printer_id=:printer AND created_at>:cutoff"),
            {**params, 'printer': printer_id,
             'cutoff': datetime.now(timezone.utc) - timedelta(hours=6)})
        identity = error_identity(message)
        return any(error_identity(row[0]) == identity for row in rows)
    return False
