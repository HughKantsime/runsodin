"""
Centralized SQLite access for monitor daemons.

Monitor processes (mqtt_monitor, moonraker_monitor, prusalink_monitor,
elegoo_monitor, vision_monitor) and supporting modules (printer_events,
smart_plug, mqtt_republish, quiet_hours, ws_hub) all need raw sqlite3
connections. This module provides a context manager that enforces:

  - busy_timeout=10000  (wait up to 10s for WAL locks instead of failing)
  - Proper cleanup on exceptions (conn.close() in finally)
  - Optional row_factory for dict-style row access

Usage:
    from core.db_utils import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ...")
        conn.commit()

Copied to core/ as part of the modular architecture refactor.
Old import path (from db_utils import get_db) continues to work via re-exports in db_utils.py.
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DATABASE_PATH", "/data/odin.db")


@contextmanager
def get_db(row_factory=None):
    """Yield a sqlite3 connection with busy_timeout and guaranteed cleanup.

    Args:
        row_factory: Optional row factory (e.g. sqlite3.Row) for dict-style access.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        yield conn
    finally:
        conn.close()
