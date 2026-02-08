#!/usr/bin/env python3
"""
Add WebSocket real-time updates to O.D.I.N.
- Backend: FastAPI WebSocket endpoint at /ws
- IPC: MQTT monitor writes events to /tmp/odin_ws_events via file-based ring buffer
- Frontend: useWebSocket hook updates React Query cache directly
"""
import os
import json

BASE = "/opt/printfarm-scheduler"
BACKEND = f"{BASE}/backend"
FRONTEND = f"{BASE}/frontend/src"

# =============================================================================
# 1. WebSocket Hub (shared IPC via file)
# =============================================================================

ws_hub_content = '''"""
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
    line = json.dumps(event) + "\\n"
    
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
'''

with open(f"{BACKEND}/ws_hub.py", "w") as f:
    f.write(ws_hub_content)
print("✅ Created ws_hub.py")

# =============================================================================
# 2. Add WebSocket endpoint to main.py
# =============================================================================

main_path = f"{BACKEND}/main.py"
with open(main_path, "r") as f:
    main = f.read()

# Add WebSocket import
if "WebSocket" not in main:
    main = main.replace(
        "from fastapi import FastAPI, Depends, HTTPException, Query, status, Header, Request, Response, UploadFile, File",
        "from fastapi import FastAPI, Depends, HTTPException, Query, status, Header, Request, Response, UploadFile, File, WebSocket, WebSocketDisconnect"
    )
    print("✅ Added WebSocket import")

# Add asyncio import if not present
if "import asyncio" not in main:
    main = main.replace(
        "import shutil",
        "import shutil\nimport asyncio"
    )
    print("✅ Added asyncio import")

# Add /ws to auth exemption
if '"/ws"' not in main:
    main = main.replace(
        'if request.url.path in ("/health", "/metrics")',
        'if request.url.path in ("/health", "/metrics", "/ws")'
    )
    print("✅ Added /ws to auth exemption")

# Add the WebSocket endpoint (insert before the last static files mount or at end)
ws_endpoint = '''

# ============== WebSocket Real-Time Updates ==============

class ConnectionManager:
    """Manages active WebSocket connections."""
    
    def __init__(self):
        self.active: list[WebSocket] = []
    
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
    
    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
    
    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = ConnectionManager()

async def ws_broadcaster():
    """Background task: read events from hub file, broadcast to WebSocket clients."""
    from ws_hub import read_events_since
    import time
    
    last_ts = time.time()
    
    while True:
        await asyncio.sleep(1)  # Check every 1 second
        
        if not ws_manager.active:
            # No clients connected, just advance timestamp
            last_ts = time.time()
            continue
        
        events, last_ts = read_events_since(last_ts)
        
        for evt in events:
            await ws_manager.broadcast(evt)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket endpoint for real-time printer telemetry and job updates.
    
    Pushes events:
    - printer_telemetry: {printer_id, bed_temp, nozzle_temp, state, progress, ...}
    - job_update: {printer_id, job_name, status, progress, layer, ...}  
    - alert_new: {count} (new unread alert count)
    """
    await ws_manager.connect(ws)
    try:
        while True:
            # Keep connection alive, handle client messages (ping/pong)
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                # Client can send "ping" to keep alive
                if data == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                # Send server-side ping to keep connection alive
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_manager.disconnect(ws)
'''

# Add WebSocket endpoint and start broadcaster in lifespan
if "ConnectionManager" not in main:
    # Find the /metrics endpoint and insert after it
    metrics_end = main.find('\n@app.get("/metrics"')
    if metrics_end == -1:
        # Fallback: insert before the last chunk
        metrics_end = main.rfind("\n# ==============")
    
    # Find the end of the metrics function
    # Insert the WS code right before @app.get("/metrics")
    # Actually, let's just append before static files or at end
    
    # Find a good insertion point - after the Prometheus endpoint
    # Search for the end of prometheus_metrics function
    prom_idx = main.find('async def prometheus_metrics')
    if prom_idx != -1:
        # Find the next @app. or # ===== after it
        search_from = prom_idx + 100
        next_decorator = main.find('\n@app.', search_from)
        next_section = main.find('\n# =====', search_from)
        
        candidates = [x for x in [next_decorator, next_section] if x != -1]
        if candidates:
            insert_at = min(candidates)
        else:
            insert_at = len(main)
        
        main = main[:insert_at] + ws_endpoint + main[insert_at:]
        print("✅ Added WebSocket endpoint")
    else:
        # Fallback: append
        main += ws_endpoint
        print("✅ Added WebSocket endpoint (appended)")
    
    # Add broadcaster to lifespan
    old_lifespan = """async def lifespan(app: FastAPI):
    \"\"\"Initialize database on startup.\"\"\"
    Base.metadata.create_all(bind=engine)
    yield"""
    
    new_lifespan = """async def lifespan(app: FastAPI):
    \"\"\"Initialize database on startup, start WebSocket broadcaster.\"\"\"
    Base.metadata.create_all(bind=engine)
    # Start WebSocket event broadcaster
    broadcast_task = asyncio.create_task(ws_broadcaster())
    yield
    broadcast_task.cancel()"""
    
    if old_lifespan in main:
        main = main.replace(old_lifespan, new_lifespan)
        print("✅ Updated lifespan with WS broadcaster")
    else:
        print("⚠️  Could not find exact lifespan block — add manually")

with open(main_path, "w") as f:
    f.write(main)

# =============================================================================
# 3. Wire MQTT monitor to push events via ws_hub
# =============================================================================

mqtt_path = f"{BACKEND}/mqtt_monitor.py"
with open(mqtt_path, "r") as f:
    mqtt = f.read()

# Add ws_hub import
if "ws_hub" not in mqtt:
    mqtt = mqtt.replace(
        "import printer_events",
        "import printer_events\ntry:\n    from ws_hub import push_event as ws_push\nexcept ImportError:\n    def ws_push(*a, **kw): pass"
    )
    print("✅ Added ws_hub import to mqtt_monitor")

# Find the telemetry UPDATE SQL and add ws_push after it
# The key is the block where it writes bed_temp, nozzle_temp etc to DB
# Look for the commit after the big UPDATE printers SET
if "ws_push" not in mqtt:
    # Strategy: find where telemetry is committed and add ws_push after
    # The telemetry update is around line 212 based on our grep
    # Look for the pattern where it does the UPDATE and then commits
    
    # Find: " bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
    telemetry_marker = 'bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,'
    idx = mqtt.find(telemetry_marker)
    
    if idx != -1:
        # Find the conn.commit() after this UPDATE
        commit_after = mqtt.find('conn.commit()', idx)
        if commit_after != -1:
            # Find the end of this line
            eol = mqtt.find('\n', commit_after)
            
            # Insert ws_push call after the commit
            ws_push_code = '''
                    # Push telemetry to WebSocket clients
                    ws_push("printer_telemetry", {
                        "printer_id": self.printer_id,
                        "bed_temp": bed_t,
                        "bed_target": bed_target,
                        "nozzle_temp": noz_t,
                        "nozzle_target": noz_target,
                        "state": gstate,
                        "progress": self._state.get('mc_percent'),
                        "remaining_min": self._state.get('mc_remaining_time'),
                        "current_layer": self._state.get('layer_num'),
                        "total_layers": self._state.get('total_layer_num'),
                        "lights": self._state.get('lights_report', [{}])[0].get('mode', 'unknown') if self._state.get('lights_report') else None,
                    })'''
            
            mqtt = mqtt[:eol] + ws_push_code + mqtt[eol:]
            print("✅ Added telemetry ws_push to mqtt_monitor")
        else:
            print("⚠️  Could not find conn.commit() after telemetry UPDATE")
    else:
        print("⚠️  Could not find telemetry UPDATE marker")

# Add ws_push for state changes (job start/complete/fail)
# Find _on_state_change method and add pushes for key transitions
state_change_marker = 'def _on_state_change(self'
sc_idx = mqtt.find(state_change_marker)
if sc_idx != -1 and 'ws_push("job_update"' not in mqtt:
    # Find the end of _on_state_change method — look for next def or unindented line
    # Actually, let's just hook into job_started and job_completed in printer_events
    # That's cleaner — we'll add ws_push to printer_events instead
    pass

with open(mqtt_path, "w") as f:
    f.write(mqtt)

# =============================================================================
# 4. Wire printer_events.py to push job events and alerts via ws_hub
# =============================================================================

pe_path = f"{BACKEND}/printer_events.py"
with open(pe_path, "r") as f:
    pe = f.read()

if "ws_hub" not in pe:
    # Add import at top
    pe = pe.replace(
        'log = logging.getLogger("printer_events")',
        'log = logging.getLogger("printer_events")\n\ntry:\n    from ws_hub import push_event as ws_push\nexcept ImportError:\n    def ws_push(*a, **kw): pass'
    )
    print("✅ Added ws_hub import to printer_events")

# Add ws_push to dispatch_alert
if 'ws_push("alert_new"' not in pe:
    pe = pe.replace(
        'log.debug(f"Dispatched alert \'{title}\' to {len(users)} users")',
        '''log.debug(f"Dispatched alert '{title}' to {len(users)} users")
        
        # Push to WebSocket
        ws_push("alert_new", {
            "alert_type": alert_type,
            "severity": severity,
            "title": title,
            "message": message,
            "printer_id": printer_id,
            "job_id": job_id,
            "count": len(users),
        })'''
    )
    print("✅ Added ws_push to dispatch_alert")

# Add ws_push to job_started
if 'ws_push("job_started"' not in pe:
    pe = pe.replace(
        'log.info(f"Job started on printer {printer_id}: {job_name} (print_jobs.id={job_id})")',
        '''log.info(f"Job started on printer {printer_id}: {job_name} (print_jobs.id={job_id})")
        ws_push("job_started", {
            "printer_id": printer_id,
            "job_name": job_name,
            "print_job_id": job_id,
        })'''
    )
    print("✅ Added ws_push to job_started")

# Add ws_push to job_completed
if 'ws_push("job_completed"' not in pe:
    pe = pe.replace(
        'log.info(f"Job {status} on printer {printer_id}: {job_name}")',
        '''log.info(f"Job {status} on printer {printer_id}: {job_name}")
        ws_push("job_completed", {
            "printer_id": printer_id,
            "job_name": job_name,
            "status": status,
            "print_job_id": print_job_id,
            "scheduled_job_id": scheduled_job_id,
        })'''
    )
    print("✅ Added ws_push to job_completed")

with open(pe_path, "w") as f:
    f.write(pe)

# =============================================================================
# 5. Frontend: useWebSocket hook
# =============================================================================

ws_hook = '''import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'

/**
 * WebSocket hook for real-time O.D.I.N. updates.
 * 
 * Connects to /ws endpoint. On receiving events, updates React Query cache
 * directly so existing components re-render without polling.
 * 
 * Event types handled:
 * - printer_telemetry: updates printer data in cache
 * - job_started / job_completed: invalidates job queries
 * - alert_new: invalidates alert queries
 */
export default function useWebSocket() {
  const queryClient = useQueryClient()
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const lastConnectAttempt = useRef(0)

  const connect = useCallback(() => {
    // Don't reconnect too fast
    const now = Date.now()
    if (now - lastConnectAttempt.current < 3000) return
    lastConnectAttempt.current = now

    // Build WebSocket URL from current location
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws`

    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        console.log('[WS] Connected')
        if (reconnectTimer.current) {
          clearTimeout(reconnectTimer.current)
          reconnectTimer.current = null
        }
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          
          if (msg.type === 'ping') {
            ws.send('ping')
            return
          }

          const d = msg.data
          if (!d) return

          switch (msg.type) {
            case 'printer_telemetry':
              // Update the specific printer in the cached printers list
              queryClient.setQueryData(['printers'], (old) => {
                if (!old || !Array.isArray(old)) return old
                return old.map(p => {
                  if (p.id !== d.printer_id) return p
                  return {
                    ...p,
                    bed_temp: d.bed_temp ?? p.bed_temp,
                    bed_target_temp: d.bed_target ?? p.bed_target_temp,
                    nozzle_temp: d.nozzle_temp ?? p.nozzle_temp,
                    nozzle_target_temp: d.nozzle_target ?? p.nozzle_target_temp,
                    gcode_state: d.state ?? p.gcode_state,
                    last_seen: new Date().toISOString(),
                  }
                })
              })
              // Update print jobs progress
              if (d.progress != null) {
                queryClient.setQueryData(['print-jobs'], (old) => {
                  if (!old || !Array.isArray(old)) return old
                  return old.map(j => {
                    if (j.printer_id !== d.printer_id || j.status !== 'running') return j
                    return {
                      ...j,
                      progress_percent: d.progress ?? j.progress_percent,
                      remaining_minutes: d.remaining_min ?? j.remaining_minutes,
                      current_layer: d.current_layer ?? j.current_layer,
                      total_layers: d.total_layers ?? j.total_layers,
                    }
                  })
                })
              }
              break

            case 'job_started':
            case 'job_completed':
              // Refetch jobs and printers — state changed significantly
              queryClient.invalidateQueries({ queryKey: ['print-jobs'] })
              queryClient.invalidateQueries({ queryKey: ['jobs'] })
              queryClient.invalidateQueries({ queryKey: ['printers'] })
              queryClient.invalidateQueries({ queryKey: ['stats'] })
              break

            case 'alert_new':
              // Refetch alert counts
              queryClient.invalidateQueries({ queryKey: ['dash-alert-summary'] })
              queryClient.invalidateQueries({ queryKey: ['alert-summary'] })
              queryClient.invalidateQueries({ queryKey: ['alerts'] })
              break
          }
        } catch (e) {
          // Ignore parse errors
        }
      }

      ws.onclose = () => {
        console.log('[WS] Disconnected, reconnecting in 5s...')
        wsRef.current = null
        reconnectTimer.current = setTimeout(connect, 5000)
      }

      ws.onerror = () => {
        // onclose will fire after this
        ws.close()
      }

    } catch (e) {
      console.error('[WS] Connection failed:', e)
      reconnectTimer.current = setTimeout(connect, 5000)
    }
  }, [queryClient])

  useEffect(() => {
    connect()
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
      }
    }
  }, [connect])
}
'''

with open(f"{FRONTEND}/hooks/useWebSocket.js", "w") as f:
    f.write(ws_hook)
print("✅ Created useWebSocket hook")

# =============================================================================
# 6. Wire useWebSocket into App.jsx
# =============================================================================

app_path = f"{FRONTEND}/App.jsx"
with open(app_path, "r") as f:
    app = f.read()

if "useWebSocket" not in app:
    # Add import
    # Find the last import line
    lines = app.split('\n')
    last_import = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            last_import = i
    
    lines.insert(last_import + 1, "import useWebSocket from './hooks/useWebSocket'")
    app = '\n'.join(lines)
    
    # Add the hook call inside the App component
    # Find "export default function App" or "function App"
    # and add useWebSocket() as the first line in the function body
    
    # Look for the pattern: function App() { or export default function App() {
    import re
    match = re.search(r'(function App\s*\([^)]*\)\s*\{)', app)
    if match:
        insert_after = match.end()
        app = app[:insert_after] + '\n  useWebSocket()\n' + app[insert_after:]
        print("✅ Added useWebSocket() to App component")
    else:
        # Try arrow function: const App = () => {
        match = re.search(r'((?:const|let)\s+App\s*=\s*(?:\([^)]*\)|[^=])*=>\s*\{)', app)
        if match:
            insert_after = match.end()
            app = app[:insert_after] + '\n  useWebSocket()\n' + app[insert_after:]
            print("✅ Added useWebSocket() to App component (arrow fn)")
        else:
            print("⚠️  Could not find App component — add useWebSocket() manually")

    with open(app_path, "w") as f:
        f.write(app)

# =============================================================================
# 7. Reduce polling intervals (WebSocket is primary, polling is fallback)
# =============================================================================

dash_path = f"{FRONTEND}/pages/Dashboard.jsx"
with open(dash_path, "r") as f:
    dash = f.read()

# Change refetchIntervals to longer fallback values
# printers: 5000 -> 30000 (30s fallback, WS handles real-time)
# print-jobs: 3000 -> 30000
# alerts: 15000 -> 60000
changes = [
    ("queryFn: () => printers.list(true), refetchInterval: 5000",
     "queryFn: () => printers.list(true), refetchInterval: 30000"),
    ("queryFn: () => printJobs.list({ limit: 20 }), refetchInterval: 3000",
     "queryFn: () => printJobs.list({ limit: 20 }), refetchInterval: 30000"),
]

for old, new in changes:
    if old in dash:
        dash = dash.replace(old, new)
        print(f"✅ Reduced polling: {old[:50]}...")

# Alert polling - two separate queries
dash = dash.replace(
    "queryFn: alertsApi.summary, refetchInterval: 15000",
    "queryFn: alertsApi.summary, refetchInterval: 60000"
)
# The other alert-summary query at line 296 has a 15000 interval too
dash = dash.replace(
    "refetchInterval: 15000,",
    "refetchInterval: 60000,"
)

with open(dash_path, "w") as f:
    f.write(dash)

# =============================================================================
# 8. Ensure hooks directory exists
# =============================================================================

hooks_dir = f"{FRONTEND}/hooks"
os.makedirs(hooks_dir, exist_ok=True)

print("\n" + "=" * 60)
print("✅ WebSocket implementation complete!")
print("=" * 60)
print("""
Deploy steps:
  cd /opt/printfarm-scheduler
  python3 add_websocket.py
  cd frontend && npm run build
  systemctl restart printfarm-backend
  systemctl restart printfarm-monitor

What this does:
  1. ws_hub.py — File-based IPC between monitor and FastAPI
  2. /ws endpoint — FastAPI WebSocket with connection manager
  3. Background broadcaster — Reads hub file every 1s, pushes to clients
  4. MQTT monitor — Pushes telemetry events on every status update
  5. printer_events.py — Pushes job start/complete and alert events
  6. useWebSocket hook — Frontend connects once, updates React Query cache
  7. Dashboard polling reduced to 30s fallback (WebSocket is primary)

Architecture:
  MQTT Printer → mqtt_monitor.py → ws_hub.py (file IPC)
                                        ↓
  Browser ← WebSocket ← FastAPI broadcaster (1s poll of file)
                                        
  React Query cache updated directly — no component changes needed
""")
