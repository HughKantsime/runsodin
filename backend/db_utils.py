"""
Centralized SQLite access for monitor daemons.

Re-export facade: canonical location is now backend/core/db_utils.py.
All existing `from db_utils import get_db` imports continue to work.
"""

from core.db_utils import DB_PATH, get_db  # noqa: F401
