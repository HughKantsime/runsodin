"""
WebSocket Event Hub — IPC between monitor processes and FastAPI WebSocket.

Re-export facade: canonical location is now backend/core/ws_hub.py.
All existing `from ws_hub import ...` imports continue to work.
"""

from core.ws_hub import (  # noqa: F401
    ensure_table,
    push_event,
    read_events_since,
    _cleanup,
)
