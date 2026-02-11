"""
PrusaLink Monitor — Polling daemon for Prusa 3D printers.

Supports: MK4/S, MK3.9, MK3.5, MINI+, XL, CORE One
Protocol: PrusaLink REST API (v1)
Auth: HTTP Digest or API key

Architecture:
  - One thread per PrusaLink printer
  - Polls /api/v1/status every POLL_INTERVAL seconds
  - Feeds printer_events.py universal handler (same as Bambu/Moonraker)
  - WebSocket push, MQTT republish, telemetry DB updates

This is a clone of moonraker_monitor.py adapted for PrusaLink's API shape.
"""

import os
import sys
import time
import logging
import sqlite3
import threading
from datetime import datetime, timezone

import printer_events

# WebSocket push (same as mqtt_monitor / moonraker_monitor)
try:
    from ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

# MQTT republish (same as mqtt_monitor / moonraker_monitor)
try:
    import mqtt_republish as _mqtt_republish
except ImportError:
    _mqtt_republish = None

from prusalink_adapter import PrusaLinkPrinter, PrusaLinkState

log = logging.getLogger(__name__)

DB_PATH = os.environ.get(
    "DATABASE_PATH",
    "/data/odin.db",
)
POLL_INTERVAL = 10  # seconds


class PrusaLinkMonitorThread(threading.Thread):
    """Monitor thread for a single PrusaLink printer."""

    def __init__(self, printer_id: int, name: str, host: str, port: int = 80,
                 username: str = "maker", password: str = "", api_key: str = ""):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.name = name
        self.host = host
        self.client = PrusaLinkPrinter(
            host=host, port=port,
            username=username, password=password, api_key=api_key
        )
        self._running = True
        self._last_heartbeat = 0
        self._last_state = None
        self._last_filename = None
        self._last_progress = 0.0

    def stop(self):
        self._running = False

    def run(self):
        log.info(f"[{self.name}] PrusaLink monitor started for {self.host}")
        # Auto-discover camera on startup
        self._discover_and_save_camera()
        while self._running:
            try:
                status = self.client.get_status()
                self._process_status(status)
            except Exception as e:
                log.warning(f"[{self.name}] Poll error: {e}")
            time.sleep(POLL_INTERVAL)
        log.info(f"[{self.name}] PrusaLink monitor stopped")

    def _discover_and_save_camera(self):
        """Discover camera URL and save to DB via printer_events."""
        try:
            cam_url = self.client.get_webcam_url()
            if cam_url:
                printer_events.discover_camera(self.printer_id, cam_url)
                log.info(f"[{self.name}] Camera discovered: {cam_url}")
        except Exception as e:
            log.warning(f"[{self.name}] Camera discovery failed: {e}")

    def _process_status(self, status):
        """Process polled status — update DB, detect transitions, push events."""

        # ----------------------------------------------------------
        # 1. Detect state transitions via printer_events.py
        # ----------------------------------------------------------
        current_state = status.internal_state
        current_file = status.filename

        # Map PrusaLink states to O.D.I.N. states
        if status.state == PrusaLinkState.PRINTING:
            odin_state = "RUNNING"
        elif status.state == PrusaLinkState.PAUSED:
            odin_state = "PAUSE"
        elif status.state in (PrusaLinkState.FINISHED, PrusaLinkState.STOPPED):
            odin_state = "FINISH"
        elif status.state == PrusaLinkState.ERROR:
            odin_state = "FAILED"
        elif status.state == PrusaLinkState.ATTENTION:
            odin_state = "PAUSE"  # Attention = waiting for user (filament change, etc.)
        else:
            odin_state = "IDLE"

        # Only fire events on transitions
        if current_state != self._last_state:
            if odin_state == "RUNNING" and self._last_state != "PRINTING":
                # Print started
                printer_events.on_print_start(
                    self.printer_id,
                    current_file or "Unknown",
                    0,  # total_layers — PrusaLink doesn't always provide
                    None,  # layer count
                    status.time_remaining // 60 if status.time_remaining else None,
                )
            elif odin_state == "FINISH":
                # Print completed
                printer_events.on_print_complete(
                    self.printer_id,
                    current_file or self._last_filename or "Unknown",
                    status.time_printing,
                    None,  # filament_used_g — not provided
                )
            elif odin_state == "FAILED":
                # Print failed
                printer_events.on_print_failed(
                    self.printer_id,
                    current_file or self._last_filename or "Unknown",
                    "Printer reported error state",
                )
            elif odin_state == "PAUSE":
                printer_events.on_print_paused(self.printer_id)

            self._last_state = current_state

        # Track filename for completion events
        if current_file:
            self._last_filename = current_file

        # ----------------------------------------------------------
        # 2. Update progress (while printing)
        # ----------------------------------------------------------
        if status.state == PrusaLinkState.PRINTING and status.progress_percent > 0:
            if abs(status.progress_percent - self._last_progress) >= 1.0:
                remaining_min = status.time_remaining // 60 if status.time_remaining else None
                printer_events.on_progress_update(
                    self.printer_id,
                    status.progress_percent,
                    status.current_layer,
                    status.total_layers,
                    remaining_min,
                )
                self._last_progress = status.progress_percent

        # ----------------------------------------------------------
        # 3. Telemetry + heartbeat (throttled to every 10 seconds)
        # ----------------------------------------------------------
        if time.time() - self._last_heartbeat >= 10:
            try:
                conn = sqlite3.connect(DB_PATH)

                bed_t = status.bed_temp
                bed_tt = status.bed_target
                noz_t = status.nozzle_temp
                noz_tt = status.nozzle_target
                gstate = status.internal_state
                progress = status.progress_percent
                remaining_min = status.time_remaining // 60 if status.time_remaining else None
                current_layer = status.current_layer
                total_layers = status.total_layers

                # Determine stage
                if status.state == PrusaLinkState.PRINTING:
                    stage = "Printing"
                elif status.state == PrusaLinkState.PAUSED:
                    stage = "Paused"
                elif status.state == PrusaLinkState.ATTENTION:
                    stage = "Attention"
                elif noz_tt and noz_tt > 0:
                    stage = "Heating hotend"
                elif bed_tt and bed_tt > 0:
                    stage = "Heating bed"
                else:
                    stage = "Idle"

                conn.execute(
                    "UPDATE printers SET last_seen=datetime('now'),"
                    " bed_temp=COALESCE(?,bed_temp),bed_target_temp=COALESCE(?,bed_target_temp),"
                    " nozzle_temp=COALESCE(?,nozzle_temp),nozzle_target_temp=COALESCE(?,nozzle_target_temp),"
                    " gcode_state=COALESCE(?,gcode_state),print_stage=COALESCE(?,print_stage) WHERE id=?",
                    (bed_t, bed_tt, noz_t, noz_tt, gstate, stage, self.printer_id)
                )
                conn.commit()

                # WebSocket push to frontend
                ws_push('printer_telemetry', {
                    'printer_id': self.printer_id,
                    'bed_temp': bed_t,
                    'bed_target': bed_tt,
                    'nozzle_temp': noz_t,
                    'nozzle_target': noz_tt,
                    'state': gstate,
                    'progress': progress,
                    'remaining_min': remaining_min,
                    'current_layer': current_layer,
                    'total_layers': total_layers,
                })

                # MQTT republish to external broker
                if _mqtt_republish:
                    try:
                        _mqtt_republish.republish_telemetry(self.printer_id, self.name, {
                            "bed_temp": bed_t, "bed_target": bed_tt,
                            "nozzle_temp": noz_t, "nozzle_target": noz_tt,
                            "state": gstate,
                            "progress": progress,
                            "remaining_min": remaining_min,
                            "current_layer": current_layer,
                            "total_layers": total_layers,
                        })
                    except Exception:
                        pass

                conn.close()
                self._last_heartbeat = time.time()

            except Exception as e:
                log.warning(f"[{self.name}] Telemetry update failed: {e}")


# ------------------------------------------------------------------
# Main — standalone daemon mode (like moonraker_monitor.py)
# ------------------------------------------------------------------
def start_prusalink_monitors():
    """
    Load PrusaLink printers from DB and start monitor threads.
    Called from main.py on startup, same as start_moonraker_monitors().
    """
    threads = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, name, api_host, api_key FROM printers WHERE api_type='prusalink' AND api_host IS NOT NULL AND is_active=1"
        ).fetchall()
        conn.close()

        for row in rows:
            printer_id = row["id"]
            name = row["name"]
            host = row["api_host"]
            api_key_raw = row["api_key"] or ""

            # Decrypt credentials if encrypted (same as Moonraker)
            username = "maker"
            password = ""
            api_key = ""
            if api_key_raw:
                try:
                    from crypto import decrypt
                    decrypted = decrypt(api_key_raw)
                    # Format: "username|password" or just "api_key"
                    if "|" in decrypted:
                        username, password = decrypted.split("|", 1)
                    else:
                        api_key = decrypted
                except Exception:
                    # Not encrypted or decrypt failed — use as raw API key
                    api_key = api_key_raw

            t = PrusaLinkMonitorThread(
                printer_id=printer_id,
                name=name,
                host=host,
                username=username,
                password=password,
                api_key=api_key,
            )
            t.start()
            threads.append(t)
            log.info(f"Started PrusaLink monitor for {name} ({host})")

    except Exception as e:
        log.error(f"Failed to start PrusaLink monitors: {e}")

    return threads


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # Daemon mode: keep running even if no printers exist yet.
    while True:
        threads = start_prusalink_monitors()
        if not threads:
            log.info("No PrusaLink printers found in database — sleeping 60s")
            time.sleep(60)
            continue

        log.info(f"Monitoring {len(threads)} PrusaLink printer(s)")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            for t in threads:
                t.stop()
            break
