"""
WebSocket Event Hub — IPC between monitor processes and FastAPI WebSocket.

Monitor processes (mqtt_monitor, moonraker_monitor, etc.) call push_event()
to write events to a shared SQLite table. The FastAPI WebSocket handler
reads new events by ID and broadcasts them.

Replaces the previous file-based IPC (/tmp/odin_ws_events with fcntl locks)
with a reliable SQLite table that never silently drops events.

Copied to core/ as part of the modular architecture refactor.
Old import path (from ws_hub import ...) continues to work via re-exports in ws_hub.py.
"""

import json
import time
import logging
from typing import List, Tuple

from core.db_utils import get_db

log = logging.getLogger("ws_hub")

_CLEANUP_INTERVAL = 30   # seconds between cleanup runs
_EVENT_TTL = 60           # delete events older than this (seconds)
_last_cleanup = 0


def ensure_table():
    """Create ws_events table if it doesn't exist. Called from main.py lifespan."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ws_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ws_events_created ON ws_events(created_at)"
        )
        conn.commit()


def push_event(event_type: str, data: dict):
    """
    Called by monitor processes to publish an event.
    Signature unchanged from the file-based version — monitors need zero changes.
    """
    try:
        payload = json.dumps({"type": event_type, "data": data})
        with get_db() as conn:
            conn.execute(
                "INSERT INTO ws_events (event_type, data, created_at) VALUES (?, ?, ?)",
                (event_type, payload, time.time()),
            )
            conn.commit()
    except Exception:
        pass  # Non-critical — don't crash monitors


def read_events_since(last_id: int) -> Tuple[List[dict], int]:
    """
    Read events with id > last_id.
    Returns (events_list, new_last_id).
    """
    global _last_cleanup

    try:
        with get_db() as conn:
            cur = conn.execute(
                "SELECT id, data FROM ws_events WHERE id > ? ORDER BY id",
                (last_id,),
            )
            rows = cur.fetchall()

        events = []
        newest_id = last_id
        for row_id, payload in rows:
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
            newest_id = row_id

        # Periodic cleanup
        now = time.time()
        if now - _last_cleanup > _CLEANUP_INTERVAL:
            _last_cleanup = now
            _cleanup(now - _EVENT_TTL)

        return events, newest_id

    except Exception:
        return [], last_id


def _cleanup(before_ts: float):
    """Delete events older than the given timestamp."""
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM ws_events WHERE created_at < ?", (before_ts,))
            conn.commit()
    except Exception:
        pass
