"""Text settings stored in system_config.value (a JSON column) on both dialects.

Settings written by the quiet-hours and MQTT-republish config routes are plain
text ("true", "22:00", "broker.example.test"). SQLite stores them raw, as it
always has. PostgreSQL's json column rejects raw text such as 22:00, so there
the value is bound as a JSON string. Reads accept every stored generation.
"""
import json

from sqlalchemy import JSON, bindparam, text


def config_text(value) -> str:
    """Text of a stored setting: raw SQLite text, a quoted JSON string read
    back as text, or a PostgreSQL-decoded JSON value (str or bool)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
    return "" if value is None else str(value)


def write_config_text(db, key: str, value: str) -> None:
    """Insert or update one text setting in the caller's transaction."""
    statement_value = bindparam("v", type_=JSON) if db.get_bind().dialect.name == "postgresql" else bindparam("v")
    existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": key}).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = :v WHERE key = :k").bindparams(statement_value),
                   {"v": value, "k": key})
    else:
        db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)").bindparams(statement_value),
                   {"k": key, "v": value})
