"""Case-tolerant preferences and repeat suppression shared by API and daemons."""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text


def preferences_for(db, alert_type):
    """One preference per user; canonical lowercase wins over legacy duplicates.

    Select scalar columns so legacy enum names never reach SQLAlchemy's strict
    enum decoder. Within one spelling, the latest saved preference wins.
    """
    kind = getattr(alert_type, 'value', alert_type).lower()
    rows = db.execute(text(
        "SELECT id,user_id,in_app,browser_push,email,threshold_value FROM alert_preferences "
        "WHERE LOWER(alert_type)=:kind "
        "ORDER BY CASE WHEN alert_type=:kind THEN 0 ELSE 1 END, id DESC"), {'kind': kind}).mappings()
    users = {}
    for row in rows:
        users.setdefault(row['user_id'], SimpleNamespace(**row))
    return list(users.values())


def error_identity(message):
    message = message or ''
    prefix = re.match(r'^\[[^\]\s:]+:[^\]]+\]', message)
    return prefix.group(0) if prefix else message


def active_resource_duplicate(db, user_id, alert_type, printer_id, spool_id, message=''):
    """Unread/undismissed spool alerts persist; printer identities expire at 6h."""
    kind = getattr(alert_type, 'value', alert_type).lower()
    params = {'user': user_id, 'kind': kind}
    base = ("SELECT message FROM alerts WHERE user_id=:user AND LOWER(alert_type)=:kind "
            "AND is_read IS FALSE AND is_dismissed IS FALSE ")
    if kind == 'spool_low' and spool_id is not None:
        return db.execute(text(base + 'AND spool_id=:spool LIMIT 1'),
                          {**params, 'spool': spool_id}).first() is not None
    if kind == 'printer_error' and printer_id is not None:
        rows = db.execute(text(base + 'AND printer_id=:printer AND created_at>:cutoff'),
                          {**params, 'printer': printer_id,
                           'cutoff': datetime.now(timezone.utc) - timedelta(hours=6)})
        identity = error_identity(message)
        return any(error_identity(row[0]) == identity for row in rows)
    return False
