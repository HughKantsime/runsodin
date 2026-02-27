"""
Elegoo SDCP Monitor — WebSocket daemon for Elegoo 3D printers.

Supports: Centauri Carbon (FDM), Neptune 4 series, Saturn series (resin)
Protocol: SDCP v3.0.0 over WebSocket
Transport: ws://printer_ip:3030/websocket (no auth)

Architecture:
  - One thread per Elegoo printer
  - WebSocket connection receives push status updates (no polling needed!)
  - Feeds printer_events.py universal handler (same as Bambu/Moonraker/PrusaLink)
  - WebSocket push to frontend, MQTT republish, telemetry DB updates

Key difference from PrusaLink/Moonraker:
  SDCP printers PUSH status to us via WebSocket, not pull.
  We don't need to poll — just listen. Status arrives every few seconds.
"""

import os
import sys
import time
import logging
import sqlite3
import threading
from datetime import datetime, timezone

from core.db_utils import get_db

# WebSocket push (same as all other monitors)
try:
    from core.ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

# MQTT republish (same as all other monitors)
try:
    import modules.notifications.mqtt_republish as _mqtt_republish
except ImportError:
    _mqtt_republish = None

from modules.printers.adapters.elegoo import ElegooPrinter, ElegooStatus, SDCPCurrentStatus, SDCPPrintStatus

log = logging.getLogger(__name__)

RECONNECT_INTERVAL = 30  # seconds between reconnect attempts


class ElegooMonitorThread(threading.Thread):
    """Monitor thread for a single Elegoo SDCP printer."""

    def __init__(self, printer_id: int, name: str, host: str,
                 port: int = 3030, mainboard_id: str = ""):
        super().__init__(daemon=True)
        self.printer_id = printer_id
        self.name = name
        self.host = host
        self.client = ElegooPrinter(
            host=host, port=port, mainboard_id=mainboard_id
        )
        self._running = True
        self._last_heartbeat = 0
        self._last_state = None
        self._last_filename = None
        self._last_progress = 0.0
        self._last_telemetry_insert = 0
        self._last_ams_env = 0
        self._marked_offline = False

        # Register status callback on the adapter
        self.client.on_status(self._on_status_update)

    def stop(self):
        self._running = False
        self.client.disconnect()

    def run(self):
        log.info(f"[{self.name}] Elegoo SDCP monitor started for {self.host}")
        camera_discovered = False

        while self._running:
            # Connect (or reconnect)
            if not self.client._connected:
                log.info(f"[{self.name}] Connecting to {self.host}...")
                connected = self.client.connect()
                if not connected:
                    log.warning(f"[{self.name}] Connection failed, retrying in {RECONNECT_INTERVAL}s")
                    time.sleep(RECONNECT_INTERVAL)
                    continue
                # Auto-discover camera on first connect
                if not camera_discovered:
                    self._discover_and_save_camera()
                    camera_discovered = True

            # WebSocket is event-driven — just sleep and check connection health
            time.sleep(10)

            # Heartbeat: if no status update for 60s, try reconnecting
            if self._last_heartbeat > 0 and time.time() - self._last_heartbeat > 60:
                log.warning(f"[{self.name}] No status update for 60s, reconnecting...")
                # Mark printer offline in DB (same as Bambu/Moonraker pattern)
                if not self._marked_offline:
                    self._marked_offline = True
                    try:
                        with get_db() as conn:
                            conn.execute("UPDATE printers SET gcode_state='OFFLINE' WHERE id=?", (self.printer_id,))
                            conn.commit()
                    except Exception:
                        pass
                camera_discovered = False  # Re-discover camera on reconnect
                self.client.disconnect()
                time.sleep(2)

        log.info(f"[{self.name}] Elegoo SDCP monitor stopped")

    def _discover_and_save_camera(self):
        """Discover camera URL and save to DB via printer_events."""
        import modules.notifications.event_dispatcher as printer_events
        try:
            cam_url = self.client.get_webcam_url()
            if cam_url:
                printer_events.discover_camera(self.printer_id, cam_url)
                log.info(f"[{self.name}] Camera discovered: {cam_url}")
        except Exception as e:
            log.warning(f"[{self.name}] Camera discovery failed: {e}")

    def _on_status_update(self, status: ElegooStatus):
        """Called by the adapter whenever a status WebSocket message arrives."""
        try:
            self._process_status(status)
        except Exception as e:
            log.warning(f"[{self.name}] Status processing error: {e}")

    def _process_status(self, status: ElegooStatus):
        """Process status update — detect transitions, update DB, push events."""
        import modules.notifications.event_dispatcher as printer_events

        # ----------------------------------------------------------
        # 1. Detect state transitions via printer_events.py
        # ----------------------------------------------------------
        current_state = status.internal_state

        # Map to O.D.I.N. states
        if current_state == "PRINTING":
            odin_state = "RUNNING"
        elif current_state == "PAUSED":
            odin_state = "PAUSE"
        elif current_state == "FINISHED":
            odin_state = "FINISH"
        elif current_state in ("STOPPING",):
            odin_state = "FINISH"
        elif current_state in ("HEATING", "HOMING", "LEVELING"):
            odin_state = "RUNNING"  # Pre-print activity
        else:
            odin_state = "IDLE"

        # Only fire events on transitions
        if current_state != self._last_state:
            if odin_state == "RUNNING" and self._last_state not in ("PRINTING", "HEATING", "HOMING", "LEVELING"):
                # Print started
                remaining_min = status.time_remaining // 60 if status.time_remaining else None
                printer_events.on_print_start(
                    self.printer_id,
                    status.filename or "Unknown",
                    status.total_layers,
                    None,  # layer count from file
                    remaining_min,
                )
            elif odin_state == "FINISH" and self._last_state in ("PRINTING", "HEATING"):
                # Print completed
                printer_events.on_print_complete(
                    self.printer_id,
                    status.filename or self._last_filename or "Unknown",
                    status.current_ticks,
                    None,  # filament_used_g
                )
            elif odin_state == "IDLE" and self._last_state in ("PRINTING", "HEATING", "HOMING", "LEVELING"):
                # Went from active print to idle without FINISH — print failed
                printer_events.on_print_failed(
                    self.printer_id,
                    status.filename or self._last_filename or "Unknown",
                    "Print stopped unexpectedly",
                )
            elif current_state == "PAUSED" and self._last_state != "PAUSED":
                printer_events.on_print_paused(self.printer_id)

            self._last_state = current_state

        # Track filename
        if status.filename:
            self._last_filename = status.filename

        # ----------------------------------------------------------
        # 2. Progress updates (while printing)
        # ----------------------------------------------------------
        if current_state == "PRINTING" and status.progress_percent > 0:
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
            self._marked_offline = False  # Got a status update, clear offline flag
            try:
                with get_db() as conn:
                    bed_t = status.bed_temp
                    bed_tt = status.bed_target
                    noz_t = status.nozzle_temp
                    noz_tt = status.nozzle_target
                    gstate = status.internal_state
                    progress = status.progress_percent
                    remaining_min = status.time_remaining // 60 if status.time_remaining else None
                    current_layer = status.current_layer
                    total_layers = status.total_layers
                    fan_speed_val = status.model_fan  # Primary fan speed (0-100)

                    # Stage determination
                    if current_state == "PRINTING":
                        stage = "Printing"
                    elif current_state == "PAUSED":
                        stage = "Paused"
                    elif current_state == "HEATING":
                        stage = "Heating"
                    elif current_state == "HOMING":
                        stage = "Homing"
                    elif current_state == "LEVELING":
                        stage = "Leveling"
                    else:
                        stage = "Idle"

                    conn.execute(
                        "UPDATE printers SET last_seen=datetime('now'),"
                        " bed_temp=COALESCE(?,bed_temp),bed_target_temp=COALESCE(?,bed_target_temp),"
                        " nozzle_temp=COALESCE(?,nozzle_temp),nozzle_target_temp=COALESCE(?,nozzle_target_temp),"
                        " gcode_state=COALESCE(?,gcode_state),print_stage=COALESCE(?,print_stage),"
                        " fan_speed=COALESCE(?,fan_speed) WHERE id=?",
                        (bed_t, bed_tt, noz_t, noz_tt, gstate, stage, fan_speed_val, self.printer_id)
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
                        'fan_speed': fan_speed_val,
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
                                "fan_speed": fan_speed_val,
                            })
                        except Exception:
                            pass

                    # ---- Timeseries Telemetry Capture ----
                    # Record temps + fan speed every 60s during active prints
                    if gstate in ('PRINTING', 'HEATING', 'PAUSED') and time.time() - self._last_telemetry_insert >= 60:
                        self._last_telemetry_insert = time.time()
                        try:
                            conn.execute(
                                "INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed) VALUES (?, ?, ?, ?, ?, ?)",
                                (self.printer_id, bed_t, noz_t, bed_tt, noz_tt, fan_speed_val)
                            )
                            conn.execute("DELETE FROM printer_telemetry WHERE recorded_at < datetime('now', '-90 days')")
                            conn.commit()
                        except Exception as e:
                            log.debug(f"[{self.name}] Telemetry insert: {e}")

                    # ---- Enclosure Environmental Data Capture ----
                    # Record box/enclosure temp every 5 minutes (same table as AMS env data)
                    if time.time() - self._last_ams_env >= 300:
                        self._last_ams_env = time.time()
                        try:
                            box_temp = status.box_temp
                            if box_temp and box_temp > 0:
                                conn.execute(
                                    "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (?, ?, ?, ?)",
                                    (self.printer_id, 0, None, box_temp)
                                )
                                conn.execute("DELETE FROM ams_telemetry WHERE recorded_at < datetime('now', '-90 days')")
                                conn.commit()
                        except Exception as e:
                            log.debug(f"[{self.name}] Enclosure env capture: {e}")

                self._last_heartbeat = time.time()

            except Exception as e:
                log.warning(f"[{self.name}] Telemetry update failed: {e}")


# ------------------------------------------------------------------
# Main — standalone daemon mode
# ------------------------------------------------------------------
def start_elegoo_monitors():
    """
    Load Elegoo printers from DB and start monitor threads.
    Called from main.py on startup.
    """
    threads = []
    try:
        with get_db(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT id, name, api_host, api_key FROM printers WHERE api_type='elegoo' AND api_host IS NOT NULL AND is_active=1"
            ).fetchall()

        for row in rows:
            printer_id = row["id"]
            name = row["name"]
            host = row["api_host"]
            mainboard_id = ""

            # api_key stores mainboard_id for Elegoo (no auth needed)
            if row["api_key"]:
                try:
                    from core.crypto import decrypt
                    mainboard_id = decrypt(row["api_key"])
                except Exception:
                    mainboard_id = row["api_key"]

            t = ElegooMonitorThread(
                printer_id=printer_id,
                name=name,
                host=host,
                mainboard_id=mainboard_id,
            )
            t.start()
            threads.append(t)
            log.info(f"Started Elegoo monitor for {name} ({host})")

    except Exception as e:
        log.error(f"Failed to start Elegoo monitors: {e}")

    return threads

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # Optional: run discovery first
    if "--discover" in sys.argv:
        print("Discovering Elegoo printers on network...")
        printers = ElegooPrinter.discover()
        if printers:
            for p in printers:
                print(f"  Found: {p['name']} ({p['machine_name']}) at {p['ip']} — FW {p['firmware']}")
        else:
            print("  No printers found.")
        sys.exit(0)

    # Daemon mode: keep running even if no printers exist yet.
    while True:
        threads = start_elegoo_monitors()
        if not threads:
            log.info("No Elegoo printers found in database — sleeping 60s")
            time.sleep(60)
            continue

        log.info(f"Monitoring {len(threads)} Elegoo printer(s)")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            for t in threads:
                t.stop()
            break
