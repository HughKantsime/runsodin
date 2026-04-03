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


# ---------------------------------------------------------------------------
# Event bus integration
# ---------------------------------------------------------------------------

# Events that have dedicated handlers with legacy name translation.
# The wildcard handler skips these to avoid double-publishing.
_TRANSLATED_EVENTS = frozenset()


def _handle_job_started(event) -> None:
    """Forward job.started as 'job_started' for WebSocket frontend compatibility."""
    push_event("job_started", event.data)


def _handle_job_completed(event) -> None:
    """Forward job.completed / job.failed as 'job_completed' for frontend compatibility."""
    push_event("job_completed", event.data)


def _handle_alert_dispatched(event) -> None:
    """Forward notifications.alert_dispatched as 'alert_new' for frontend compatibility."""
    push_event("alert_new", event.data)


def _handle_other_events(event) -> None:
    """
    Catch-all handler for events that do not need name translation.
    Forwards the event to ws_events under its canonical event_type.
    Skips events handled by dedicated translators to avoid duplicates.
    """
    if event.event_type not in _TRANSLATED_EVENTS:
        push_event(event.event_type, event.data)


def subscribe_to_bus(bus) -> None:
    """
    Register ws_hub as a subscriber on the event bus.

    Called once from main.py lifespan after the bus singleton is ready.
    """
    global _TRANSLATED_EVENTS
    from core import events as ev

    # Job lifecycle events — translate to legacy ws event names the frontend expects
    bus.subscribe(ev.JOB_STARTED, _handle_job_started)
    bus.subscribe(ev.JOB_COMPLETED, _handle_job_completed)
    bus.subscribe(ev.JOB_FAILED, _handle_job_completed)

    # Alert events — translate to legacy ws event name
    bus.subscribe("notifications.alert_dispatched", _handle_alert_dispatched)

    # Record which events have dedicated translators so the wildcard skips them
    _TRANSLATED_EVENTS = frozenset({
        ev.JOB_STARTED,
        ev.JOB_COMPLETED,
        ev.JOB_FAILED,
        "notifications.alert_dispatched",
    })

    # All other events forwarded as-is (printer.*, vision.*, inventory.*, system.*)
    bus.subscribe("*", _handle_other_events)

    log.debug("ws_hub subscribed to event bus")
