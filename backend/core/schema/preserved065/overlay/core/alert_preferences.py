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
        "WHERE LOWER(CAST(alert_type AS TEXT))=:kind "
        "ORDER BY CASE WHEN CAST(alert_type AS TEXT)=:kind THEN 0 ELSE 1 END, id DESC"), {'kind': kind}).mappings()
    users = {}
    for row in rows:
        users.setdefault(row['user_id'], SimpleNamespace(**row))
    return list(users.values())


def seed_attention_preferences(db) -> bool:
    """Add missing defaults on upgrade; preserve every explicit user opt-out.

    Return whether the alert transaction has new preferences to commit.
    """
    inserted = 0
    for kind in ('print_paused', 'print_cancelled', 'printer_error'):
        # PostgreSQL infers the inserted value as alerttype and the comparison as
        # text. Keep separate binds so neither context dictates the other's type.
        result = db.execute(text(
            "INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email) "
            "SELECT u.id,:kind,TRUE,TRUE,FALSE FROM users u "
            "WHERE NOT EXISTS (SELECT 1 FROM alert_preferences p "
            "WHERE p.user_id=u.id AND LOWER(CAST(p.alert_type AS TEXT))=:match_kind)"
        ), {'kind': kind, 'match_kind': kind})
        inserted += result.rowcount
    return inserted > 0
