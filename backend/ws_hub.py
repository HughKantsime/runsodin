"""
WebSocket Event Hub - IPC between monitor processes and FastAPI WebSocket.

Monitor processes (mqtt_monitor, moonraker_monitor) call push_event() to write
events to a shared file. The FastAPI WebSocket handler reads and broadcasts.

Uses a simple JSON-lines file as a ring buffer. Lock-free: monitors append,
FastAPI reads and truncates.
"""
import os
import json
import time
import fcntl
from typing import List, Dict, Any

EVENT_FILE = "/tmp/odin_ws_events"
MAX_EVENTS = 200  # Keep last N events in file


def push_event(event_type: str, data: dict):
    """
    Called by monitor processes to publish an event.
    Appends a JSON line to the event file.
    """
    event = {
        "type": event_type,
        "data": data,
        "ts": time.time()
    }
    line = json.dumps(event) + "\n"
    
    try:
        fd = os.open(EVENT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, line.encode())
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except (BlockingIOError, OSError):
        pass  # Skip if locked — non-critical


def read_events_since(last_ts: float) -> tuple:
    """
    Read events newer than last_ts.
    Returns (events_list, new_last_ts).
    """
    try:
        if not os.path.exists(EVENT_FILE):
            return [], last_ts
        
        with open(EVENT_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            lines = f.readlines()
            fcntl.flock(f, fcntl.LOCK_UN)
        
        events = []
        newest_ts = last_ts
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                if evt.get("ts", 0) > last_ts:
                    events.append(evt)
                    newest_ts = max(newest_ts, evt["ts"])
            except json.JSONDecodeError:
                continue
        
        # Truncate file if too large
        if len(lines) > MAX_EVENTS * 2:
            try:
                with open(EVENT_FILE, "w") as f:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    f.writelines(lines[-MAX_EVENTS:])
                    fcntl.flock(f, fcntl.LOCK_UN)
            except (BlockingIOError, OSError):
                pass
        
        return events, newest_ts
    
    except (BlockingIOError, OSError, FileNotFoundError):
        return [], last_ts
