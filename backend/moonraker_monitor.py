"""
Moonraker Printer Monitor

Polls Moonraker REST API for printer status, similar to how
PrinterMonitor subscribes to Bambu MQTT. Designed to plug into
the existing MQTTMonitorDaemon alongside Bambu monitors.

Handles:
- Periodic status polling (every 3 seconds)
- Print job start/end detection and DB logging
- Progress tracking (percent, layers)
- Filament slot sync (ACE MMU)
- Reconnection on failure
"""

import os
import sys
import sqlite3
import time
import logging
import threading
from datetime import datetime
from typing import Optional, Dict, Any

from moonraker_adapter import MoonrakerPrinter, MoonrakerState
import printer_events

# WebSocket push (same as mqtt_monitor)
try:
    from ws_hub import broadcast as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

# MQTT republish (same as mqtt_monitor)
try:
    from mqtt_republish import get_republisher
    mqtt_republish = get_republisher()
except Exception:
    mqtt_republish = None

log = logging.getLogger("moonraker_monitor")

DB_PATH = os.environ.get(
    "PRINTFARM_DB",
    "/data/odin.db",
)

POLL_INTERVAL = 3          # seconds between status polls
RECONNECT_INTERVAL = 30    # seconds between reconnection attempts
PROGRESS_DB_INTERVAL = 5   # seconds between progress DB writes (throttle)


class MoonrakerMonitor:
    """
    Monitors a single Moonraker printer via REST polling.
    
    Drop-in complement to the MQTT-based PrinterMonitor class.
    Same DB tables, same state tracking, same job logging.
    """
    
    def __init__(self, printer_id: int, name: str, host: str, port: int = 80, api_key: str = ""):
        self.printer_id = printer_id
        self.name = name
        self.host = host
        self.port = port
        
        self.printer = MoonrakerPrinter(host=host, port=port, api_key=api_key)
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._prev_state: Optional[str] = None
        self._current_job_db_id: Optional[int] = None
        self._last_progress_write = 0.0
        self._last_filename = ""
    
    # ==================== Lifecycle ====================
    
    def connect(self) -> bool:
        """Connect to the printer and start polling thread."""
        if self.printer.connect():
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop,
                name=f"moonraker-{self.name}",
                daemon=True,
            )
            self._thread.start()
            return True
        return False
    
    def disconnect(self):
        """Stop polling and disconnect."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.printer.disconnect()
        log.info(f"[{self.name}] Disconnected")
    
    # ==================== Poll Loop ====================
    
    def _poll_loop(self):
        """Main polling loop — runs in its own thread."""
        while self._running:
            try:
                status = self.printer.get_status()
                self._process_status(status)
            except Exception as e:
                log.error(f"[{self.name}] Poll error: {e}")
                # Try to reconnect
                self._handle_disconnect()
            
            time.sleep(POLL_INTERVAL)
    
    def _handle_disconnect(self):
        """Handle lost connection — attempt reconnection."""
        self.printer._connected = False
        log.warning(f"[{self.name}] Connection lost, retrying in {RECONNECT_INTERVAL}s")
        
        while self._running:
            time.sleep(RECONNECT_INTERVAL)
            try:
                if self.printer.connect():
                    log.info(f"[{self.name}] Reconnected")
                    return
            except Exception:
                pass
            log.warning(f"[{self.name}] Reconnect failed, retrying...")
    
    # ==================== Status Processing ====================
    
    def _process_status(self, status):
        """Process a status update — detect state changes, track jobs."""
        # Update telemetry + heartbeat (throttled to every 10 seconds)
        import time as _time
        if _time.time() - getattr(self, '_last_heartbeat', 0) >= 10:
            try:
                import sqlite3
                conn = sqlite3.connect(DB_PATH)
                bed_t = bed_tt = noz_t = noz_tt = None
                gstate = None
                stage = 'Idle'
                progress = None
                remaining_min = None
                current_layer = None
                total_layers = None

                if hasattr(status, 'raw_data') and status.raw_data:
                    rd = status.raw_data
                    hb = rd.get('heater_bed', {})
                    ext = rd.get('extruder', {})
                    bed_t = hb.get('temperature')
                    bed_tt = hb.get('target')
                    noz_t = ext.get('temperature')
                    noz_tt = ext.get('target')
                    ps = rd.get('print_stats', {})
                    mk_state = ps.get('state', '')
                    gstate = mk_state.upper() if mk_state else None
                    if mk_state == 'printing': stage = 'Printing'
                    elif mk_state == 'paused': stage = 'Paused'
                    elif noz_tt and noz_tt > 0: stage = 'Heating hotend'
                    elif bed_tt and bed_tt > 0: stage = 'Heating bed'

                # Progress data
                progress = status.progress_percent
                current_layer = status.current_layer
                total_layers = status.total_layers

                # Calculate remaining time from elapsed + progress
                if status.print_duration and status.print_duration > 0 and progress and progress > 1:
                    elapsed_min = status.print_duration / 60.0
                    remaining_min = round(elapsed_min * (100.0 - progress) / progress)
                    if remaining_min < 0:
                        remaining_min = 0

                conn.execute(
                    "UPDATE printers SET last_seen=datetime('now'),"
                    " bed_temp=COALESCE(?,bed_temp),bed_target_temp=COALESCE(?,bed_target_temp),"
                    " nozzle_temp=COALESCE(?,nozzle_temp),nozzle_target_temp=COALESCE(?,nozzle_target_temp),"
                    " gcode_state=COALESCE(?,gcode_state),print_stage=COALESCE(?,print_stage) WHERE id=?",
                    (bed_t, bed_tt, noz_t, noz_tt, gstate, stage, self.printer_id))
                conn.commit()

                # WebSocket push to frontend (same as Bambu monitor)
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

                # MQTT republish to external broker (same as Bambu monitor)
                if mqtt_republish:
                    try:
                        mqtt_republish.republish_telemetry(self.printer_id, self.name, {
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
                self._last_heartbeat = _time.time()
            except Exception as e:
                log.warning(f"Failed to update telemetry for {self.name}: {e}")
        
        internal_state = status.internal_state
        
        # Detect state change
        if internal_state != self._prev_state:
            log.info(f"[{self.name}] State: {self._prev_state} -> {internal_state}")
            
            # Job started
            if internal_state == "RUNNING" and self._prev_state != "PAUSE":
                self._job_started(status)
            
            # Job paused
            elif internal_state == "PAUSE" and self._prev_state == "RUNNING":
                log.info(f"[{self.name}] Print paused")
            
            # Job resumed
            elif internal_state == "RUNNING" and self._prev_state == "PAUSE":
                log.info(f"[{self.name}] Print resumed")
            
            # Job ended (was running/paused, now idle/error)
            elif self._prev_state in ("RUNNING", "PAUSE") and internal_state in ("IDLE", "FAILED"):
                end_status = "completed" if internal_state == "IDLE" else "failed"
                self._job_ended(end_status, status)
            
            self._prev_state = internal_state
        
        # Progress updates while printing
        if internal_state == "RUNNING" and self._current_job_db_id:
            self._update_progress(status)
    
    # ==================== Job Tracking ====================
    
    def _job_started(self, status):
        """Record a new print job starting."""
        filename = status.filename or "Unknown"
        total_layers = status.total_layers
        bed_target = status.bed_target
        nozzle_target = status.nozzle_target
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO print_jobs 
                (printer_id, job_id, filename, job_name, started_at, status,
                 total_layers, bed_temp_target, nozzle_temp_target)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """, (
                self.printer_id,
                f"mk_{int(time.time())}",
                filename,
                filename,
                datetime.now().isoformat(),
                total_layers,
                bed_target,
                nozzle_target,
            ))
            self._current_job_db_id = cur.lastrowid
            self._last_filename = filename
            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job started: {filename} (DB id: {self._current_job_db_id})")
            
            # Attempt auto-link to scheduled job (same logic as Bambu monitor)
            self._try_auto_link(filename, total_layers)
            
        except Exception as e:
            log.error(f"[{self.name}] Failed to record job start: {e}")
    
    def _job_ended(self, end_status: str, status):
        """Record a print job ending."""
        if not self._current_job_db_id:
            log.warning(f"[{self.name}] Job ended but no current job tracked")
            return
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                UPDATE print_jobs 
                SET ended_at = ?, status = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), end_status, self._current_job_db_id))
            
            # If linked to a scheduled job, update it too
            cur.execute("""
                SELECT scheduled_job_id FROM print_jobs WHERE id = ?
            """, (self._current_job_db_id,))
            row = cur.fetchone()
            if row and row[0]:
                sched_status = "completed" if end_status == "completed" else "failed"
                cur.execute("""
                    UPDATE jobs SET status = ? WHERE id = ?
                """, (sched_status, row[0]))
                log.info(f"[{self.name}] Scheduled job #{row[0]} marked {sched_status}")
                
                # Auto-deduct filament if completed
                if end_status == "completed":
                    self._auto_deduct_filament(cur, row[0])
            
            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job {end_status}: DB id {self._current_job_db_id}")
            
            # Increment care counters on successful completion
            if end_status == "completed":
                try:
                    conn2 = sqlite3.connect(DB_PATH)
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT started_at, ended_at FROM print_jobs WHERE id = ?", (self._current_job_db_id,))
                    pj_row = cur2.fetchone()
                    if pj_row and pj_row[0] and pj_row[1]:
                        from datetime import datetime as dt
                        started = dt.fromisoformat(pj_row[0])
                        ended = dt.fromisoformat(pj_row[1])
                        duration_sec = (ended - started).total_seconds()
                        printer_events.increment_care_counters(self.printer_id, duration_sec / 3600.0, 1)
                    conn2.close()
                except Exception as ce:
                    log.warning(f"[{self.name}] Failed to update care counters: {ce}")
            
            # Dispatch alerts
            job_name = self._last_filename or "Unknown"
            if end_status == "completed":
                printer_events.dispatch_alert(
                    alert_type="print_complete",
                    severity="success",
                    title=f"Print Complete: {job_name}",
                    message=f"Finished on {self.name}",
                    printer_id=self.printer_id,
                )
            else:
                printer_events.dispatch_alert(
                    alert_type="print_failed",
                    severity="error",
                    title=f"Print Failed: {job_name}",
                    message=f"Failed on {self.name}",
                    printer_id=self.printer_id,
                )
                printer_events.record_error(
                    printer_id=self.printer_id,
                    error_code="PRINT_FAILED",
                    error_message=f"Print failed: {job_name}",
                    source="moonraker",
                    severity="error",
                    create_alert=False,
                )
            
            self._current_job_db_id = None
            self._last_filename = ""
            
        except Exception as e:
            log.error(f"[{self.name}] Failed to record job end: {e}")
    
    def _update_progress(self, status):
        """Update progress in DB (throttled)."""
        now = time.time()
        if now - self._last_progress_write < PROGRESS_DB_INTERVAL:
            return
        
        self._last_progress_write = now
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                UPDATE print_jobs
                SET progress_percent = ?, current_layer = ?, remaining_minutes = ?
                WHERE id = ?
            """, (
                status.progress_percent,
                status.current_layer,
                round(status.print_duration * (100.0 - status.progress_percent) / max(status.progress_percent, 0.1) / 60.0) if status.print_duration and status.progress_percent and status.progress_percent > 1 else None,
                self._current_job_db_id,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[{self.name}] Progress update failed: {e}")
    
    # ==================== Job Auto-Linking ====================
    
    def _try_auto_link(self, filename: str, total_layers: int):
        """
        Try to link this MQTT-detected print to a scheduled job.
        Same two-strategy approach as Bambu monitor:
          1. Name match
          2. Layer count fingerprint
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            # Get candidates: scheduled/pending jobs for this printer
            cur.execute("""
                SELECT j.id as job_id, j.status, pf.filename, pf.original_filename,
                       pf.layer_count, m.name as model_name
                FROM jobs j
                JOIN print_files pf ON j.print_file_id = pf.id
                JOIN models m ON pf.model_id = m.id
                WHERE j.printer_id = ?
                  AND j.status IN ('scheduled', 'pending')
            """, (self.printer_id,))
            
            candidates = cur.fetchall()
            if not candidates:
                conn.close()
                return
            
            # Strategy 1: Name match
            fname_lower = filename.lower() if filename else ""
            for c in candidates:
                match_names = [
                    (c["filename"] or "").lower(),
                    (c["original_filename"] or "").lower(),
                    (c["model_name"] or "").lower(),
                ]
                if fname_lower and any(fname_lower in n or n in fname_lower for n in match_names if n):
                    self._link_job(cur, c["job_id"])
                    conn.commit()
                    conn.close()
                    log.info(f"[{self.name}] Auto-linked to job #{c['job_id']} (name match)")
                    return
            
            # Strategy 2: Layer count fingerprint
            if total_layers and total_layers > 0:
                # Deduplicate by job_id
                layer_matches = {}
                for c in candidates:
                    if c["layer_count"] == total_layers and c["job_id"] not in layer_matches:
                        layer_matches[c["job_id"]] = c
                
                if len(layer_matches) == 1:
                    job_id = list(layer_matches.keys())[0]
                    self._link_job(cur, job_id)
                    conn.commit()
                    conn.close()
                    log.info(f"[{self.name}] Auto-linked to job #{job_id} (layer count: {total_layers})")
                    return
                elif len(layer_matches) > 1:
                    log.warning(
                        f"[{self.name}] Ambiguous layer match ({total_layers} layers) "
                        f"— {len(layer_matches)} candidates, skipping auto-link"
                    )
            
            conn.close()
            
        except Exception as e:
            log.warning(f"[{self.name}] Auto-link failed: {e}")
    
    def _link_job(self, cur, scheduled_job_id: int):
        """Link the current print_job to a scheduled job."""
        if self._current_job_db_id:
            cur.execute("""
                UPDATE print_jobs SET scheduled_job_id = ? WHERE id = ?
            """, (scheduled_job_id, self._current_job_db_id))
            cur.execute("""
                UPDATE jobs SET status = 'printing' WHERE id = ?
            """, (scheduled_job_id,))
    
    def _auto_deduct_filament(self, cur, scheduled_job_id: int):
        """Auto-deduct filament weight when a linked job completes."""
        try:
            cur.execute("""
                SELECT j.spool_id, pf.filament_weight_grams
                FROM jobs j
                JOIN print_files pf ON j.print_file_id = pf.id
                WHERE j.id = ?
            """, (scheduled_job_id,))
            row = cur.fetchone()
            
            if row and row[0] and row[1]:
                spool_id = row[0]
                grams_used = row[1]
                
                cur.execute("""
                    UPDATE spools 
                    SET remaining_weight = MAX(0, remaining_weight - ?)
                    WHERE id = ?
                """, (grams_used, spool_id))
                
                # Log to spool_usage
                cur.execute("""
                    INSERT INTO spool_usage (spool_id, job_id, printer_id, grams_used, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (spool_id, scheduled_job_id, self.printer_id, grams_used,
                      datetime.now().isoformat()))
                
                log.info(f"[{self.name}] Deducted {grams_used}g from spool #{spool_id}")
                
        except Exception as e:
            log.warning(f"[{self.name}] Filament deduction failed: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # Daemon mode: keep running even if no printers exist yet.
    while True:
        threads = start_moonraker_monitors()
        if not threads:
            log.info("No Moonraker printers found in database — sleeping 60s")
            time.sleep(60)
            continue

        log.info(f"Monitoring {len(threads)} Moonraker printer(s)")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            for t in threads:
                t.stop()
            break
