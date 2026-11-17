"""Read the Education-mode switch the same way on SQLite and PostgreSQL.

``system_config.value`` is a JSON column. The switch is written as the text
``true`` or ``false``: SQLite hands that text back, PostgreSQL the boolean.
"""
from sqlalchemy import text


def stored_true(value) -> bool:
    return str(value).strip('"').lower() == 'true'


def education_mode_enabled(db) -> bool:
    return stored_true(db.execute(text("SELECT value FROM system_config WHERE key='education_mode'")).scalar())
