"""Case-tolerant alert preferences shared by notification and push delivery."""
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
