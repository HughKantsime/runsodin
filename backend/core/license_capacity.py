"""Transaction-scoped creation serialization, shared by all creation services."""
from sqlalchemy import text


def lock_capacity(db, resource):
    if resource not in {"printers", "users"}:
        raise ValueError("Unknown licensed resource")
    key = "license_capacity_" + resource
    # Existing config storage; no schema change. A write locks the row on
    # PostgreSQL and the writer on SQLite until the caller commits/rolls back.
    db.execute(text("INSERT INTO system_config (key,value) VALUES (:key,'null') "
                    "ON CONFLICT(key) DO NOTHING"), {"key": key})
    db.execute(text("UPDATE system_config SET value=value WHERE key=:key"), {"key": key})
