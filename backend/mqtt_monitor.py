#!/usr/bin/env python3
"""
MQTT Print Monitor Daemon
Connects to all Bambu printers and tracks print jobs automatically.

Usage:
    python mqtt_monitor.py          # Run in foreground
    python mqtt_monitor.py --daemon # Run as background daemon
"""
import sys
sys.path.insert(0, '/opt/printfarm-scheduler/backend')

import os
import sqlite3
import json
import time
import signal
import logging
from datetime import datetime
from threading import Thread, Lock
from typing import Dict, Optional, Any

import crypto
from bambu_adapter import BambuPrinter

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('mqtt_monitor')

DB_PATH = '/opt/printfarm-scheduler/backend/printfarm.db'

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
        self._lock = Lock()
    
    def connect(self) -> bool:
        """Connect to printer MQTT."""
        try:
            self._bambu = BambuPrinter(
                ip=self.ip,
                serial=self.serial,
                access_code=self.access_code,
                on_status_update=self._on_status
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
            log.info(f"[{self.name}] Disconnected")
    
    def _on_status(self, status):
        """Handle incoming MQTT status update."""
        with self._lock:
            raw = status.raw_data.get('print', {})
            
            # Merge partial updates into state
            for key, value in raw.items():
                if value is not None:
                    self._state[key] = value
            
            # Update progress if we have an active job (throttled to every 5 seconds)
            if self._current_job_id and time.time() - self._last_progress_update >= 5:
                self._update_progress()
            
            # Check for state transitions
            gcode_state = self._state.get('gcode_state')
            if gcode_state and gcode_state != self._last_gcode_state:
                self._on_state_change(self._last_gcode_state, gcode_state)
                self._last_gcode_state = gcode_state
    
    def _on_state_change(self, old_state: Optional[str], new_state: str):
        """Handle print state transitions."""
        log.info(f"[{self.name}] State: {old_state} -> {new_state}")
        
        # IDLE/FINISH -> RUNNING = Job started
        if new_state == 'RUNNING' and old_state in (None, 'IDLE', 'FINISH', 'FAILED', 'PREPARE'):
            self._job_started()
        
        # RUNNING -> FINISH = Job completed
        elif new_state == 'FINISH' and old_state == 'RUNNING':
            self._job_ended('completed')
        
        # RUNNING -> FAILED = Job failed
        elif new_state == 'FAILED' and old_state == 'RUNNING':
            self._job_ended('failed')
        
        # RUNNING -> IDLE = Job cancelled
        elif new_state == 'IDLE' and old_state == 'RUNNING':
            self._job_ended('cancelled')
    
    def _job_started(self):
        """Record a new print job starting and link to scheduled job if found."""
        job_name = self._state.get('subtask_name', 'Unknown')
        filename = self._state.get('gcode_file', '')
        mqtt_job_id = self._state.get('job_id') or f"local_{int(time.time())}"
        total_layers = self._state.get('total_layer_num')
        bed_target = self._state.get('bed_target_temper')
        nozzle_target = self._state.get('nozzle_target_temper')
        
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            
            # Try to find a matching scheduled job
            # Strategy 1: Match by job_name to filename/model name
            # Strategy 2: Match by layer count (unique fingerprint)
            self._linked_job_id = None
            total_layers = self._state.get('total_layer_num', 0)
            
            cur.execute("""
                SELECT DISTINCT j.id, pf.filename, j.item_name, m.name as model_name, pf.layer_count
                FROM jobs j
                LEFT JOIN models m ON j.model_id = m.id 
                LEFT JOIN print_files pf ON m.id = pf.model_id
                WHERE j.printer_id = ? 
                AND j.status IN ('scheduled', 'pending', 'SCHEDULED', 'PENDING')
                ORDER BY j.scheduled_start ASC
                LIMIT 10
            """, (self.printer_id,))
            
            candidates = cur.fetchall()
            job_base = job_name.lower().replace('.3mf', '').replace('.gcode', '')
            
            # Strategy 1: Try name matching first
            for cand_id, cand_filename, cand_item_name, cand_model_name, cand_layers in candidates:
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
            
            if not self._linked_job_id and candidates:
                log.info(f"[{self.name}] No auto-match for '{job_base}' ({total_layers} layers) - link manually via API")
            
            # Insert print_jobs record
            cur.execute("""
                INSERT INTO print_jobs 
                (printer_id, job_id, filename, job_name, started_at, status, 
                 total_layers, bed_temp_target, nozzle_temp_target, scheduled_job_id)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """, (self.printer_id, str(mqtt_job_id), filename, job_name, 
                  datetime.now().isoformat(), total_layers, bed_target, nozzle_target,
                  self._linked_job_id))
            self._current_job_id = cur.lastrowid
            
            # Update linked job status to 'printing'
            if self._linked_job_id:
                cur.execute("UPDATE jobs SET status = 'PRINTING' WHERE id = ?", (self._linked_job_id,))
                log.info(f"[{self.name}] Updated job {self._linked_job_id} status to 'printing'")
            
            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job started: {job_name} (DB id: {self._current_job_id})")
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
            
            # Update print_jobs record
            cur.execute("""
                UPDATE print_jobs 
                SET ended_at = ?, status = ?, error_code = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), status, error_code, self._current_job_id))
            
            # Update linked scheduled job if exists
            if self._linked_job_id:
                # Map MQTT status to jobs table status
                job_status = 'COMPLETED' if status == 'completed' else 'FAILED'
                cur.execute("UPDATE jobs SET status = ? WHERE id = ?", (job_status, self._linked_job_id))
                log.info(f"[{self.name}] Updated linked job {self._linked_job_id} to '{job_status}'")
            
            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job {status}: DB id {self._current_job_id}")
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
    
    def start(self):
        """Start monitoring all printers."""
        self._running = True
        printers = self.load_printers()
        
        if not printers:
            log.error("No printers found!")
            return
        
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
        
        log.info(f"Connected to {len(self.monitors)}/{len(printers)} printers")
        
        # Keep running
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down...")
        
        self.stop()
    
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
