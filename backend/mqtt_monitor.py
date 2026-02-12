#!/usr/bin/env python3
"""
MQTT Print Monitor Daemon
Connects to all Bambu printers and tracks print jobs automatically.

Usage:
    python mqtt_monitor.py          # Run in foreground
    python mqtt_monitor.py --daemon # Run as background daemon
"""
import os
import sys
sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))

import sqlite3
import json
import time
import signal
import logging
from datetime import datetime, timezone
from threading import Thread, Lock
from typing import Dict, Optional, Any

import crypto
from bambu_adapter import BambuPrinter
import printer_events
try:
    import mqtt_republish
except ImportError:
    mqtt_republish = None
try:
    from ws_hub import push_event as ws_push
except ImportError:
    def ws_push(*a, **kw): pass

# Moonraker support (Klipper printers like Kobra S1 w/ Rinkhals)
try:
    from moonraker_monitor import MoonrakerMonitor
    MOONRAKER_AVAILABLE = True
except ImportError:
    MOONRAKER_AVAILABLE = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('mqtt_monitor')

DB_PATH = os.environ.get('DATABASE_PATH', '/data/odin.db')

class PrinterMonitor:
    """Monitors a single printer's MQTT stream."""
    
    def __init__(self, printer_id: int, name: str, ip: str, serial: str, access_code: str):
        self.printer_id = printer_id
        self.name = name
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        
        self._bambu: Optional[BambuPrinter] = None
        self._state: Dict[str, Any] = {}
        self._last_gcode_state: Optional[str] = None
        self._current_job_id: Optional[int] = None
        self._linked_job_id: Optional[int] = None  # Linked scheduled job from jobs table
        self._last_progress_update: float = 0
        self._last_spool_check: float = 0
        self._lock = Lock()
    
    def connect(self) -> bool:
        """Connect to printer MQTT."""
        try:
            self._bambu = BambuPrinter(
                ip=self.ip,
                serial=self.serial,
                access_code=self.access_code,
                on_status_update=self._on_status,
                client_id=f"odin_{self.printer_id}_{int(time.time())}"
            )
            if self._bambu.connect():
                log.info(f"[{self.name}] Connected")
                return True
            else:
                log.error(f"[{self.name}] Connection failed")
                return False
        except Exception as e:
            log.error(f"[{self.name}] Connection error: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from printer."""
        if self._bambu:
            self._bambu.disconnect()
            self._bambu = None
            log.info(f"[{self.name}] Disconnected")

    def _trigger_reschedule(self):
        """Fire-and-forget POST to /api/scheduler/run so bumped jobs get reassigned."""
        def _do():
            try:
                import urllib.request
                api_key = os.environ.get('API_KEY', '')
                req = urllib.request.Request(
                    'http://localhost:8000/api/scheduler/run',
                    data=b'{}',
                    headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
                    method='POST'
                )
                urllib.request.urlopen(req, timeout=5)
                log.info(f"[{self.name}] Triggered scheduler re-run after bump")
            except Exception as e:
                log.debug(f"[{self.name}] Scheduler trigger failed (non-critical): {e}")
        Thread(target=_do, daemon=True).start()

    def _dispatch_alert(self, alert_type: str, severity: str, title: str,
                        message: str = "", job_id: int = None, spool_id: int = None,
                        metadata: dict = None):
        """
        Create alert records for all users who have this alert type enabled.
        Uses raw SQL to avoid importing SQLAlchemy into the monitor daemon.
        Handles deduplication for spool_low alerts.
        """
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            cur = conn.cursor()

            # Get all users with preferences for this alert type
            cur.execute("""
                SELECT user_id, in_app, browser_push, email
                FROM alert_preferences
                WHERE alert_type = ? AND in_app = 1
            """, (alert_type,))
            prefs = cur.fetchall()

            # If no preferences exist, seed defaults for all active users
            if not prefs:
                cur.execute("SELECT id FROM users WHERE is_active = 1")
                users = cur.fetchall()
                defaults = {
                    'print_complete': (1, 0, 0),
                    'print_failed': (1, 1, 0),
                    'spool_low': (1, 0, 0),
                    'maintenance_overdue': (1, 0, 0),
                    'schedule_bump': (1, 0, 0),
                }
                for (uid,) in users:
                    for at, (ia, bp, em) in defaults.items():
                        cur.execute("""
                            INSERT OR IGNORE INTO alert_preferences
                            (user_id, alert_type, in_app, browser_push, email, threshold_value)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (uid, at, ia, bp, em, 100.0 if at == 'spool_low' else None))
                conn.commit()
                # Re-query
                cur.execute("""
                    SELECT user_id, in_app, browser_push, email
                    FROM alert_preferences
                    WHERE alert_type = ? AND in_app = 1
                """, (alert_type,))
                prefs = cur.fetchall()

            created = 0
            for user_id, in_app, browser_push, email in prefs:
                # Dedup: spool_low — skip if unread alert exists for same spool
                if alert_type == 'spool_low' and spool_id:
                    cur.execute("""
                        SELECT 1 FROM alerts
                        WHERE user_id = ? AND alert_type = 'SPOOL_LOW'
                        AND spool_id = ? AND is_read = 0 AND is_dismissed = 0
                        LIMIT 1
                    """, (user_id, spool_id))
                    if cur.fetchone():
                        continue

                # Create in-app alert
                cur.execute("""
                    INSERT INTO alerts
                    (user_id, alert_type, severity, title, message,
                     printer_id, job_id, spool_id, metadata_json, is_read, is_dismissed, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                """, (user_id, alert_type.upper(), severity.upper(), title, message,
                      self.printer_id, job_id, spool_id,
                      json.dumps(metadata) if metadata else None,
                      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
                created += 1

            conn.commit()
            if created > 0:
                log.info(f"[{self.name}] Alert dispatched: {alert_type} to {created} users")
        except Exception as e:
            log.error(f"[{self.name}] Failed to dispatch alert: {e}")
        finally:
            if conn:
                conn.close()

    def _on_status(self, status):
        """Handle incoming MQTT status update."""
        with self._lock:
            raw = status.raw_data.get('print', {})
            
            # Update telemetry + heartbeat (throttled to every 10 seconds)
            if time.time() - getattr(self, '_last_heartbeat', 0) >= 10:
                try:
                    conn = sqlite3.connect(DB_PATH)
                    bed_t = self._state.get('bed_temper')
                    bed_tt = self._state.get('bed_target_temper')
                    noz_t = self._state.get('nozzle_temper')
                    noz_tt = self._state.get('nozzle_target_temper')
                    gstate = self._state.get('gcode_state')
                    stg_cur = self._state.get('stg_cur', -1)
                    _smap = {0:'Idle',1:'Auto-leveling',2:'Heatbed preheating',3:'Sweeping XY',
                             4:'Changing filament',5:'Paused',6:'Filament runout',7:'Heating hotend',
                             8:'Calibrating',9:'Homing',10:'Cleaning nozzle',11:'Heating bed',
                             12:'Scanning bed',13:'First layer check',14:'Printing',255:'Idle',-1:'Idle'}
                    stage = _smap.get(stg_cur, 'Stage %s' % stg_cur)
                    # Clear stage label when not actively printing
                    if gstate not in ('RUNNING', 'PREPARE', 'PAUSE'):
                        stage = 'Idle'
                    import json as _json
                    hms_raw = self._state.get('hms', [])
                    hms_j = _json.dumps(hms_raw) if hms_raw else None
                    lights = self._state.get('lights_report', [])
                    lights_on_raw = any(l.get('mode') == 'on' for l in lights) if lights else None
                    # Check cooldown - don't overwrite if toggled via API within last 20s
                    cooldown_row = conn.execute('SELECT lights_toggled_at FROM printers WHERE id=?', (self.printer_id,)).fetchone()
                    lights_on = lights_on_raw
                    if cooldown_row and cooldown_row[0]:
                        from datetime import datetime as _dt
                        try:
                            if (_dt.utcnow() - _dt.fromisoformat(cooldown_row[0])).total_seconds() < 20:
                                lights_on = None
                        except: pass
                    noz_type = self._state.get('nozzle_type')
                    noz_dia = self._state.get('nozzle_diameter')
                    if isinstance(noz_dia, str):
                        try: noz_dia = float(noz_dia)
                        except: noz_dia = None
                    fan_speed_val = self._state.get('cooling_fan_speed')
                    conn.execute(
                        "UPDATE printers SET last_seen=datetime('now'),"
                        " bed_temp=?,bed_target_temp=?,nozzle_temp=?,nozzle_target_temp=?,"
                        " gcode_state=?,print_stage=?,hms_errors=?,lights_on=COALESCE(?,lights_on),"
                        " nozzle_type=?,nozzle_diameter=?,fan_speed=COALESCE(?,fan_speed) WHERE id=?",
                        (bed_t, bed_tt, noz_t, noz_tt, gstate, stage,
                         hms_j, lights_on, noz_type, noz_dia, fan_speed_val, self.printer_id))
                    conn.commit()
                    # Republish telemetry to external broker
                    if mqtt_republish:
                        try:
                            mqtt_republish.republish_telemetry(self.printer_id, self.name, {
                                "bed_temp": bed_t, "bed_target": bed_tt,
                                "nozzle_temp": noz_t, "nozzle_target": noz_tt,
                                "state": gstate,
                                "progress": self._state.get('mc_percent'),
                                "remaining_min": self._state.get('mc_remaining_time'),
                                "current_layer": self._state.get('layer_num'),
                                "total_layers": self._state.get('total_layer_num'),
                            })
                        except Exception:
                            pass
                    self._last_heartbeat = time.time()

                    # ---- Timeseries Telemetry Capture ----
                    # Record temps + fan speed every 60s during active prints
                    if gstate in ('RUNNING', 'PREPARE', 'PAUSE') and time.time() - getattr(self, '_last_telemetry_insert', 0) >= 60:
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

                    # ---- AMS Environmental Data Capture ----
                    # Record AMS humidity every 5 minutes (not every heartbeat)
                    if time.time() - getattr(self, '_last_ams_env', 0) >= 300:
                        self._last_ams_env = time.time()
                        try:
                            ams_raw_env = self._state.get('ams', {})
                            ams_units = ams_raw_env.get('ams', []) if isinstance(ams_raw_env, dict) else []
                            for unit_idx, unit in enumerate(ams_units):
                                if isinstance(unit, dict):
                                    humidity = unit.get('humidity')
                                    temperature = unit.get('temp')
                                    # Bambu reports humidity as string "1"-"5" or int
                                    if humidity is not None:
                                        try:
                                            humidity = int(humidity)
                                        except (ValueError, TypeError):
                                            humidity = None
                                    if temperature is not None:
                                        try:
                                            temperature = float(temperature)
                                        except (ValueError, TypeError):
                                            temperature = None
                                    if humidity is not None or temperature is not None:
                                        conn.execute(
                                            "INSERT INTO ams_telemetry (printer_id, ams_unit, humidity, temperature) VALUES (?, ?, ?, ?)",
                                            (self.printer_id, unit_idx, humidity, temperature)
                                        )
                            # Prune old data (keep 90 days)
                            conn.execute(
                                "DELETE FROM ams_telemetry WHERE recorded_at < datetime('now', '-90 days')"
                            )
                            conn.commit()
                        except Exception as e:
                            log.debug(f"[{self.name}] AMS env capture: {e}")

                    conn.close()
                    
                    # Push telemetry to WebSocket clients
                    ws_push('printer_telemetry', {
                        'printer_id': self.printer_id,
                        'bed_temp': bed_t,
                        'bed_target': bed_tt,
                        'nozzle_temp': noz_t,
                        'nozzle_target': noz_tt,
                        'state': gstate,
                        'progress': self._state.get('mc_percent'),
                        'remaining_min': self._state.get('mc_remaining_time'),
                        'current_layer': self._state.get('layer_num'),
                        'total_layers': self._state.get('total_layer_num'),
                        'gcode_file': self._state.get('subtask_name') or self._state.get('gcode_file'),
                    })
                    
                    # Process HMS errors through universal handler for alerts
                    if hms_raw:
                        # Record HMS error history on change
                        hms_key = _json.dumps(hms_raw)
                        if hms_key != getattr(self, '_last_hms_key', None):
                            self._last_hms_key = hms_key
                            try:
                                hconn = sqlite3.connect(DB_PATH)
                                parsed = printer_events.parse_hms_errors(hms_raw)
                                for err in parsed:
                                    hconn.execute(
                                        "INSERT INTO hms_error_history (printer_id, code, message, severity, source) VALUES (?, ?, ?, ?, ?)",
                                        (self.printer_id, err.get('code', ''), err.get('message', ''), err.get('severity', 'warning'), 'bambu_hms')
                                    )
                                hconn.execute("DELETE FROM hms_error_history WHERE occurred_at < datetime('now', '-90 days')")
                                hconn.commit()
                                hconn.close()
                            except Exception as e:
                                log.debug(f"[{self.name}] HMS history insert: {e}")
                        printer_events.process_hms_errors(self.printer_id, hms_raw)
                        
                except Exception as e:
                    log.warning(f"Failed to update telemetry for printer {self.printer_id}: {e}")
            
            # Check for camera URL auto-discovery (only X1C/H2D broadcast rtsp_url)
            ipcam = raw.get('ipcam', {})
            rtsp_url = ipcam.get('rtsp_url')
            if rtsp_url:
                full_url = f"rtsps://bblp:{self.access_code}@{self.ip}:322/streaming/live/1"
                printer_events.discover_camera(self.printer_id, full_url)
            
            # Merge partial updates into state
            for key, value in raw.items():
                if value is not None:
                    self._state[key] = value
            
            # Update progress if we have an active job (throttled to every 5 seconds)
            if self._current_job_id and time.time() - self._last_progress_update >= 5:
                self._update_progress()
            

            # Check AMS spool levels (throttled to every 60 seconds)
            if time.time() - self._last_spool_check >= 60:
                self._check_spool_levels()
                self._last_spool_check = time.time()
            
            # Check for state transitions
            gcode_state = self._state.get('gcode_state')
            if gcode_state and gcode_state != self._last_gcode_state:
                self._on_state_change(self._last_gcode_state, gcode_state)
                self._last_gcode_state = gcode_state
    

    def _check_spool_levels(self):
        """Check AMS spool remaining weights and fire spool_low alerts."""
        ams_data = self._state.get('ams', {})
        if not ams_data:
            return
        
        # Get threshold from first user's preference (they all share the same default)
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                SELECT threshold_value FROM alert_preferences 
                WHERE alert_type = 'spool_low' AND threshold_value IS NOT NULL
                LIMIT 1
            """)
            row = cur.fetchone()
            threshold = row[0] if row else 100.0
            conn.close()
        except Exception:
            threshold = 100.0
        
        # Check filament slots for this printer
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                SELECT fs.slot_number, fs.assigned_spool_id, s.remaining_weight_g,
                       fl.brand, fl.name as filament_name
                FROM filament_slots fs
                LEFT JOIN spools s ON fs.assigned_spool_id = s.id
                LEFT JOIN filament_library fl ON s.filament_id = fl.id
                WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
            """, (self.printer_id,))
            
            for slot_num, spool_id, remaining, brand, fil_name in cur.fetchall():
                if remaining is not None and remaining < threshold and remaining > 0:
                    spool_label = f"{brand} {fil_name}" if brand and fil_name else f"Spool #{spool_id}"
                    self._dispatch_alert(
                        alert_type='spool_low',
                        severity='warning',
                        title=f"Low Spool: {spool_label} ({self.name} slot {slot_num})",
                        message=f"{remaining:.0f}g remaining (threshold: {threshold:.0f}g)",
                        spool_id=spool_id,
                        metadata={"remaining_g": remaining, "threshold_g": threshold, "slot": slot_num}
                    )
            
            conn.close()
        except Exception as e:
            log.error(f"[{self.name}] Spool level check failed: {e}")

    def _on_state_change(self, old_state: Optional[str], new_state: str):
        """Handle print state transitions."""
        log.info(f"[{self.name}] State: {old_state} -> {new_state}")

        # Entering RUNNING or PAUSE without a tracked job = new job
        if new_state in ('RUNNING', 'PAUSE') and not self._current_job_id:
            if old_state in (None, 'IDLE', 'FINISH', 'FAILED', 'PREPARE', 'PAUSE'):
                self._job_started()

        # RUNNING/PAUSE -> FINISH = Job completed
        elif new_state == 'FINISH' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('completed')

        # RUNNING/PAUSE -> FAILED = Job failed
        elif new_state == 'FAILED' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('failed')

        # RUNNING/PAUSE -> IDLE = Job cancelled
        elif new_state == 'IDLE' and old_state in ('RUNNING', 'PAUSE'):
            self._job_ended('cancelled')
    
    def _job_started(self):
        """Record a new print job starting and link to scheduled job if found."""
        job_name = self._state.get('subtask_name', 'Unknown')
        filename = self._state.get('gcode_file', '')
        mqtt_job_id = self._state.get('job_id') or f"local_{int(time.time())}"
        total_layers = self._state.get('total_layer_num', 0)
        bed_target = self._state.get('bed_target_temper')
        nozzle_target = self._state.get('nozzle_target_temper')

        # Deferred actions — dispatched AFTER the transaction commits
        pending_alerts = []
        needs_reschedule = False

        try:
            conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
            cur = conn.cursor()
            try:
                # Grab write lock upfront so SELECT→UPDATE sequences are atomic
                cur.execute("BEGIN IMMEDIATE")

                # ---- Stale schedule cleanup (runs every _job_started, regardless of match) ----
                # scheduled_start is naive local time (written by scheduler via datetime.now())
                cur.execute("""
                    SELECT id, item_name FROM jobs
                    WHERE printer_id = ? AND status = 'SCHEDULED'
                      AND scheduled_start < datetime('now', 'localtime', '-2 hours')
                """, (self.printer_id,))
                stale_rows = cur.fetchall()
                if stale_rows:
                    stale_ids = [r[0] for r in stale_rows]
                    stale_names = [r[1] or f"job #{r[0]}" for r in stale_rows]
                    cur.execute(
                        "UPDATE jobs SET status = 'PENDING', printer_id = NULL,"
                        " scheduled_start = NULL, scheduled_end = NULL, match_score = NULL"
                        " WHERE id IN ({})".format(','.join('?' * len(stale_ids))),
                        stale_ids)
                    log.info(f"[{self.name}] Swept {len(stale_ids)} stale scheduled job(s): {', '.join(stale_names)}")
                    pending_alerts.append(dict(
                        alert_type='schedule_bump', severity='info',
                        title=f"Stale schedule swept on {self.name}",
                        message=f"Jobs reset to pending (past 2hr window): {', '.join(stale_names)}",
                        metadata={"bumped_job_ids": stale_ids, "reason": "stale_schedule"}
                    ))
                    needs_reschedule = True

                # Try to find a matching scheduled job
                # Strategy 1: Match by job_name to filename/model name
                # Strategy 2: Match by layer count (unique fingerprint)
                # Strategy 3: Sole scheduled candidate within ±2hr window
                self._linked_job_id = None

                cur.execute("""
                    SELECT DISTINCT j.id, pf.filename, j.item_name, m.name as model_name,
                           pf.layer_count, j.status, j.scheduled_start
                    FROM jobs j
                    LEFT JOIN models m ON j.model_id = m.id
                    LEFT JOIN print_files pf ON m.id = pf.model_id
                    WHERE j.printer_id = ?
                    AND j.status IN ('SCHEDULED', 'PENDING')
                    ORDER BY j.scheduled_start ASC
                    LIMIT 10
                """, (self.printer_id,))

                candidates = cur.fetchall()
                job_base = job_name.lower().replace('.3mf', '').replace('.gcode', '')

                # Strategy 1: Try name matching first
                for cand_id, cand_filename, cand_item_name, cand_model_name, cand_layers, cand_status, cand_sched in candidates:
                    match_targets = []
                    if cand_filename:
                        match_targets.append(cand_filename.lower().replace('.3mf', '').replace('.gcode', ''))
                    if cand_item_name:
                        match_targets.append(cand_item_name.lower())
                    if cand_model_name:
                        match_targets.append(cand_model_name.lower())

                    for target in match_targets:
                        if target in job_base or job_base in target:
                            self._linked_job_id = cand_id
                            log.info(f"[{self.name}] Linked to job {cand_id} by name ('{job_base}' ~ '{target}')")
                            break

                    if self._linked_job_id:
                        break

                # Strategy 2: If no name match, try layer count matching
                if not self._linked_job_id and total_layers > 0:
                    layer_matches = list({c[0]: (c[0], c[3], c[4]) for c in candidates if c[4] == total_layers}.values())
                    if len(layer_matches) == 1:
                        self._linked_job_id = layer_matches[0][0]
                        log.info(f"[{self.name}] Linked to job {self._linked_job_id} by layer count ({total_layers} layers)")
                    elif len(layer_matches) > 1:
                        log.info(f"[{self.name}] {len(layer_matches)} jobs match {total_layers} layers - cannot auto-link")

                # Strategy 3: Sole scheduled candidate within ±2hr window
                if not self._linked_job_id:
                    # Use naive local time to match scheduler's datetime.now()
                    now_local = datetime.now()
                    window_candidates = []
                    for cand_id, cand_filename, cand_item_name, cand_model_name, cand_layers, cand_status, cand_sched in candidates:
                        if cand_status != 'SCHEDULED' or not cand_sched:
                            continue
                        # Layer count sanity check: if both sides have counts, reject >20% mismatch
                        if total_layers and cand_layers and total_layers > 0 and cand_layers > 0:
                            ratio = max(total_layers, cand_layers) / min(total_layers, cand_layers)
                            if ratio > 1.2:
                                continue
                        try:
                            sched_dt = datetime.fromisoformat(cand_sched)
                            if sched_dt.tzinfo is not None:
                                sched_dt = sched_dt.replace(tzinfo=None)
                            if abs((now_local - sched_dt).total_seconds()) <= 7200:
                                window_candidates.append(cand_id)
                        except (ValueError, TypeError):
                            continue
                    if len(window_candidates) == 1:
                        self._linked_job_id = window_candidates[0]
                        log.info(f"[{self.name}] Linked to job {self._linked_job_id} by sole scheduled candidate (±2hr window)")
                    elif len(window_candidates) > 1:
                        log.info(f"[{self.name}] {len(window_candidates)} scheduled jobs in ±2hr window - cannot auto-link")

                # ---- Ad-hoc print: bump displaced scheduled jobs ----
                if not self._linked_job_id and candidates:
                    log.info(f"[{self.name}] No auto-match for '{job_base}' ({total_layers} layers) - ad-hoc print")
                    cur.execute("""
                        SELECT id, item_name FROM jobs
                        WHERE printer_id = ? AND status = 'SCHEDULED'
                    """, (self.printer_id,))
                    displaced = cur.fetchall()
                    if displaced:
                        displaced_ids = [r[0] for r in displaced]
                        displaced_names = [r[1] or f"job #{r[0]}" for r in displaced]
                        cur.execute(
                            "UPDATE jobs SET status = 'PENDING', printer_id = NULL,"
                            " scheduled_start = NULL, scheduled_end = NULL, match_score = NULL"
                            " WHERE id IN ({})".format(','.join('?' * len(displaced_ids))),
                            displaced_ids)
                        log.info(f"[{self.name}] Bumped {len(displaced_ids)} scheduled job(s) for ad-hoc print: {', '.join(displaced_names)}")
                        pending_alerts.append(dict(
                            alert_type='schedule_bump', severity='info',
                            title=f"Scheduled jobs bumped on {self.name}",
                            message=f"Ad-hoc print displaced: {', '.join(displaced_names)}. Jobs reset to pending.",
                            metadata={"bumped_job_ids": displaced_ids, "reason": "ad_hoc_print", "ad_hoc_file": job_name}
                        ))
                        needs_reschedule = True
                elif not self._linked_job_id:
                    log.info(f"[{self.name}] No auto-match for '{job_base}' ({total_layers} layers) - no candidates to bump")

                # Insert print_jobs record
                cur.execute("""
                    INSERT INTO print_jobs
                    (printer_id, job_id, filename, job_name, started_at, status,
                     total_layers, bed_temp_target, nozzle_temp_target, scheduled_job_id)
                    VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """, (self.printer_id, str(mqtt_job_id), filename, job_name,
                      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                      total_layers or None, bed_target, nozzle_target,
                      self._linked_job_id))
                new_job_id = cur.lastrowid

                # Snapshot spool weights at job start for filament usage calculation
                start_weights = {}
                try:
                    spool_rows = cur.execute("""
                        SELECT fs.slot_number, s.id, s.remaining_weight_g
                        FROM filament_slots fs
                        JOIN spools s ON fs.assigned_spool_id = s.id
                        WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
                    """, (self.printer_id,)).fetchall()
                    for slot, spool_id, weight in spool_rows:
                        if weight is not None:
                            start_weights[spool_id] = weight
                except Exception:
                    pass

                # Update linked job status to 'printing'
                if self._linked_job_id:
                    cur.execute(
                        "UPDATE jobs SET status = 'PRINTING',"
                        " actual_start = COALESCE(actual_start, ?) WHERE id = ?",
                        (datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), self._linked_job_id))
                    log.info(f"[{self.name}] Updated job {self._linked_job_id} status to 'printing'")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

            # Transaction committed — safe to update instance state
            self._current_job_id = new_job_id
            self._start_spool_weights = start_weights
            log.info(f"[{self.name}] Job started: {job_name} (DB id: {self._current_job_id})")

            # Dispatch deferred alerts and reschedule (outside transaction)
            for alert_kwargs in pending_alerts:
                self._dispatch_alert(**alert_kwargs)
            if needs_reschedule:
                self._trigger_reschedule()

        except Exception as e:
            log.error(f"[{self.name}] Failed to record job start: {e}")
    

    def _update_progress(self):
        """Update progress data for current job."""
        progress = self._state.get('mc_percent')
        remaining = self._state.get('mc_remaining_time')
        current_layer = self._state.get('layer_num')
        
        # Only update if we have meaningful data
        if progress is None and remaining is None and current_layer is None:
            return
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "UPDATE print_jobs SET progress_percent = COALESCE(?, progress_percent), "
                "remaining_minutes = COALESCE(?, remaining_minutes), "
                "current_layer = COALESCE(?, current_layer) WHERE id = ?",
                (progress, remaining, current_layer, self._current_job_id)
            )
            conn.commit()
            conn.close()
            self._last_progress_update = time.time()
        except Exception as e:
            log.error(f"[{self.name}] Failed to update progress: {e}")

    def _job_ended(self, status: str):
        """Record a print job ending and update linked scheduled job."""
        if not self._current_job_id:
            log.warning(f"[{self.name}] Job ended but no current job tracked")
            return
        
        error_code = self._state.get('print_error') if status == 'failed' else None

        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

            # Calculate duration and filament usage
            pj_row = cur.execute("SELECT started_at FROM print_jobs WHERE id = ?",
                                 (self._current_job_id,)).fetchone()
            duration_hours = None
            duration_seconds = None
            if pj_row and pj_row[0]:
                try:
                    started = datetime.fromisoformat(pj_row[0])
                    ended = datetime.fromisoformat(now_utc)
                    duration_seconds = (ended - started).total_seconds()
                    duration_hours = round(duration_seconds / 3600, 4)
                except Exception:
                    pass

            # Calculate filament used by comparing spool weights (start vs now)
            filament_used_g = None
            start_weights = getattr(self, '_start_spool_weights', {})
            if start_weights:
                try:
                    total_used = 0.0
                    spool_rows = cur.execute("""
                        SELECT s.id, s.remaining_weight_g FROM filament_slots fs
                        JOIN spools s ON fs.assigned_spool_id = s.id
                        WHERE fs.printer_id = ? AND fs.assigned_spool_id IS NOT NULL
                    """, (self.printer_id,)).fetchall()
                    for spool_id, current_weight in spool_rows:
                        if spool_id in start_weights and current_weight is not None:
                            delta = start_weights[spool_id] - current_weight
                            if delta > 0:
                                total_used += delta
                    if total_used > 0:
                        filament_used_g = round(total_used, 2)
                except Exception as e:
                    log.debug(f"[{self.name}] Filament delta calc: {e}")

            # Update print_jobs record with telemetry
            cur.execute("""
                UPDATE print_jobs
                SET ended_at = ?, status = ?, error_code = ?,
                    print_duration_seconds = ?, filament_used_g = ?
                WHERE id = ?
            """, (now_utc, status, error_code,
                  int(duration_seconds) if duration_seconds else None,
                  filament_used_g, self._current_job_id))

            # Update or create linked scheduled job
            job_name = self._state.get('subtask_name', 'Unknown')

            # SQLAlchemy Enum stores member NAMES (uppercase) for JobStatus
            status_map = {'completed': 'COMPLETED', 'failed': 'FAILED', 'cancelled': 'CANCELLED'}
            job_status = status_map.get(status, status.upper())
            if self._linked_job_id:
                cur.execute("UPDATE jobs SET status = ?, actual_end = ?, duration_hours = COALESCE(?, duration_hours) WHERE id = ?",
                            (job_status, now_utc, duration_hours, self._linked_job_id))
                log.info(f"[{self.name}] Updated linked job {self._linked_job_id} to '{job_status}' (duration={duration_hours}h)")
            else:
                # Create a jobs record for metrics tracking
                actual_start = pj_row[0] if pj_row else now_utc
                cur.execute("""
                    INSERT INTO jobs (item_name, printer_id, status, actual_start, actual_end,
                                     duration_hours, quantity, hold, is_locked, quantity_on_bed)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 0, 0, 1)
                """, (job_name, self.printer_id, job_status, actual_start, now_utc, duration_hours))
                self._linked_job_id = cur.lastrowid
                # Link the print_jobs record to this new job
                cur.execute("UPDATE print_jobs SET scheduled_job_id = ? WHERE id = ?",
                            (self._linked_job_id, self._current_job_id))
                log.info(f"[{self.name}] Created job {self._linked_job_id} for '{job_name}' ({job_status}, duration={duration_hours}h)")

            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job {status}: DB id {self._current_job_id}")

            # Dispatch alerts (use linked_job_id which references the jobs table)
            if status == 'completed':
                self._dispatch_alert(
                    alert_type='print_complete',
                    severity='info',
                    title=f"Print Complete: {job_name} ({self.name})",
                    message=f"Job finished successfully on {self.name}.",
                    job_id=self._linked_job_id,
                )
            # Increment care counters on successful completion
            if status == 'completed':
                # Calculate duration from print_jobs record
                try:
                    conn2 = sqlite3.connect(DB_PATH)
                    cur2 = conn2.cursor()
                    cur2.execute("SELECT started_at, ended_at FROM print_jobs WHERE id = ?", (self._current_job_id,))
                    row = cur2.fetchone()
                    if row and row[0] and row[1]:
                        started = datetime.fromisoformat(row[0])
                        ended = datetime.fromisoformat(row[1])
                        duration_sec = (ended - started).total_seconds()
                        printer_events.increment_care_counters(self.printer_id, duration_sec / 3600.0, 1)
                    conn2.close()
                except Exception as ce:
                    log.warning(f"[{self.name}] Failed to update care counters: {ce}")

            elif status == 'failed':
                progress = self._state.get('mc_percent', 0)
                err = self._state.get('print_error')
                msg = f"Job failed on {self.name} at {progress}% progress."
                if err:
                    msg += f" Error code: {err}"
                self._dispatch_alert(
                    alert_type='print_failed',
                    severity='critical',
                    title=f"Print Failed: {job_name} ({self.name})",
                    message=msg,
                    job_id=self._linked_job_id,
                    metadata={"progress_percent": progress, "error_code": err}
                )
                # Record error in universal format
                printer_events.record_error(
                    printer_id=self.printer_id,
                    error_code=str(err) if err else "PRINT_FAILED",
                    error_message=msg,
                    source="bambu_job",
                    severity="error",
                    create_alert=False  # Already dispatched above
                )
            
            self._current_job_id = None
            self._linked_job_id = None
        except Exception as e:
            log.error(f"[{self.name}] Failed to record job end: {e}")


class MQTTMonitorDaemon:
    """Main daemon that monitors all printers."""
    
    def __init__(self):
        self.monitors: Dict[int, PrinterMonitor] = {}
        self._running = False
    
    def load_printers(self):
        """Load Bambu printers from database."""
        # Get encryption key
        key = os.environ.get('ENCRYPTION_KEY')
        if not key:
            log.error("ENCRYPTION_KEY not set")
            return []
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('''
            SELECT id, name, model, api_host, api_key 
            FROM printers 
            WHERE api_host IS NOT NULL AND api_key IS NOT NULL AND api_key != ''
        ''')
        
        printers = []
        for row in cur.fetchall():
            try:
                decrypted = crypto.decrypt(row['api_key'])
                parts = decrypted.split('|')
                if len(parts) == 2:
                    printers.append({
                        'id': row['id'],
                        'name': row['name'],
                        'ip': row['api_host'],
                        'serial': parts[0],
                        'access_code': parts[1]
                    })
            except Exception as e:
                log.warning(f"Could not load {row['name']}: {e}")
        
        conn.close()
        return printers

    def load_moonraker_printers(self):
        """Load Moonraker-based printers from database."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, api_host
            FROM printers
            WHERE api_type = 'moonraker'
              AND api_host IS NOT NULL
              AND api_host != ''
              AND is_active = 1
        """)

        printers = []
        for row in cur.fetchall():
            host_str = row['api_host']
            # Parse host:port
            if ':' in host_str:
                host, port_str = host_str.rsplit(':', 1)
                try:
                    port = int(port_str)
                except ValueError:
                    host = host_str
                    port = 80
            else:
                host = host_str
                port = 80

            printers.append({
                'id': row['id'],
                'name': row['name'],
                'host': host,
                'port': port,
            })

        conn.close()
        return printers
    
    def start(self):
        """Start monitoring all printers."""
        self._running = True
        printers = self.load_printers()
        
        if not printers:
            log.info("No printers found yet, waiting for printers to be added...")
        
        log.info(f"Starting monitor for {len(printers)} printers")
        
        for p in printers:
            monitor = PrinterMonitor(
                printer_id=p['id'],
                name=p['name'],
                ip=p['ip'],
                serial=p['serial'],
                access_code=p['access_code']
            )
            if monitor.connect():
                self.monitors[p['id']] = monitor
        
        log.info(f"Connected to {len(self.monitors)}/{len(printers)} Bambu printers")

        # Start Moonraker monitors
        if MOONRAKER_AVAILABLE:
            mk_printers = self.load_moonraker_printers()
            if mk_printers:
                log.info(f"Starting {len(mk_printers)} Moonraker monitor(s)")
                for p in mk_printers:
                    monitor = MoonrakerMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        host=p['host'],
                        port=p['port'],
                    )
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                log.info(f"Moonraker monitors connected")
            else:
                log.info("No Moonraker printers configured")
        
        # Keep running with periodic reconnection checks
        self._all_printers = printers  # Save for reconnection
        if MOONRAKER_AVAILABLE:
            self._all_moonraker = mk_printers if mk_printers else []
        else:
            self._all_moonraker = []
        self._last_reconnect_check = time.time()
        self._last_printer_reload = time.time()
        
        try:
            while self._running:
                time.sleep(1)
                # Every 30s, check for dead connections and reconnect
                if time.time() - self._last_reconnect_check >= 30:
                    self._check_reconnect()
                    self._last_reconnect_check = time.time()
                
                # Every 60s, check for newly added printers
                if time.time() - self._last_printer_reload >= 60:
                    self._check_new_printers()
                    self._last_printer_reload = time.time()
        except KeyboardInterrupt:
            log.info("Shutting down...")
        
        self.stop()
    
    def _check_new_printers(self):
        """Check for newly added printers and connect to them."""
        try:
            current_printers = self.load_printers()
            current_ids = {p['id'] for p in current_printers}
            monitored_ids = set(self.monitors.keys())
            new_ids = current_ids - monitored_ids
            if not new_ids:
                return
            for p in current_printers:
                if p['id'] in new_ids:
                    log.info(f"New printer detected: {p['name']}, connecting...")
                    monitor = PrinterMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        ip=p['ip'],
                        serial=p['serial'],
                        access_code=p['access_code']
                    )
                    if monitor.connect():
                        self.monitors[p['id']] = monitor
                        log.info(f"[{p['name']}] Connected")
            if MOONRAKER_AVAILABLE:
                mk_printers = self.load_moonraker_printers()
                for p in mk_printers:
                    if p['id'] not in self.monitors:
                        log.info(f"New Moonraker printer: {p['name']}")
                        monitor = MoonrakerMonitor(
                            printer_id=p['id'],
                            name=p['name'],
                            host=p['host'],
                            port=p['port'],
                        )
                        if monitor.connect():
                            self.monitors[p['id']] = monitor
        except Exception as e:
            log.warning(f"Error checking for new printers: {e}")

    def _check_reconnect(self):
        """Check for dead connections and attempt reconnection."""
        # Check Bambu printers
        for p in self._all_printers:
            pid = p['id']
            monitor = self.monitors.get(pid)
            
            if monitor is None:
                # Never connected - try again
                log.info(f"[{p['name']}] Attempting initial connection...")
                new_mon = PrinterMonitor(
                    printer_id=p['id'],
                    name=p['name'],
                    ip=p['ip'],
                    serial=p['serial'],
                    access_code=p['access_code']
                )
                if new_mon.connect():
                    self.monitors[pid] = new_mon
                    log.info(f"[{p['name']}] Reconnected successfully")
                continue
            
            # Check if connection is dead
            is_dead = False
            if hasattr(monitor, '_bambu') and monitor._bambu:
                if not monitor._bambu._connected:
                    is_dead = True
            
            # Also check staleness - no heartbeat in 60s
            if not is_dead and getattr(monitor, '_last_heartbeat', 0) > 0:
                if time.time() - monitor._last_heartbeat > 120:
                    is_dead = True
            
            if is_dead:
                log.info(f"[{monitor.name}] Connection dead, reconnecting...")
                try:
                    monitor.disconnect()
                except:
                    pass
                time.sleep(1)  # let old TLS socket fully tear down

                new_mon = PrinterMonitor(
                    printer_id=p['id'],
                    name=p['name'],
                    ip=p['ip'],
                    serial=p['serial'],
                    access_code=p['access_code']
                )
                if new_mon.connect():
                    self.monitors[pid] = new_mon
                    log.info(f"[{monitor.name}] Reconnected successfully")
                else:
                    log.warning(f"[{monitor.name}] Reconnection failed, will retry in 30s")
                    del self.monitors[pid]
        
        # Check Moonraker printers
        for p in self._all_moonraker:
            pid = p['id']
            monitor = self.monitors.get(pid)
            
            if monitor is None:
                log.info(f"[{p['name']}] Attempting Moonraker connection...")
                new_mon = MoonrakerMonitor(
                    printer_id=p['id'],
                    name=p['name'],
                    host=p['host'],
                    port=p['port'],
                )
                if new_mon.connect():
                    self.monitors[pid] = new_mon
                    log.info(f"[{p['name']}] Moonraker reconnected")
                continue
            
            # Check staleness for Moonraker
            if hasattr(monitor, '_last_heartbeat') and monitor._last_heartbeat > 0:
                if time.time() - monitor._last_heartbeat > 120:
                    log.info(f"[{monitor.name}] Moonraker stale, reconnecting...")
                    try:
                        monitor.disconnect()
                    except:
                        pass
                    new_mon = MoonrakerMonitor(
                        printer_id=p['id'],
                        name=p['name'],
                        host=p['host'],
                        port=p['port'],
                    )
                    if new_mon.connect():
                        self.monitors[pid] = new_mon
                        log.info(f"[{monitor.name}] Moonraker reconnected")
                    else:
                        log.warning(f"[{monitor.name}] Moonraker reconnection failed")
                        del self.monitors[pid]
    
    def stop(self):
        """Stop all monitors."""
        self._running = False
        for monitor in self.monitors.values():
            monitor.disconnect()
        log.info("All monitors stopped")


def main():
    daemon = MQTTMonitorDaemon()
    
    # Handle signals
    def signal_handler(sig, frame):
        log.info("Signal received, stopping...")
        daemon.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    daemon.start()


if __name__ == '__main__':
    main()
