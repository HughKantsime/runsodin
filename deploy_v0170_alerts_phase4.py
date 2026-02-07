"""
v0.17.0 Phase 4 Deploy Script — Wire Alert Triggers into MQTT Monitor
Patches: mqtt_monitor.py

Adds:
  - _dispatch_alert() helper (raw SQL, no SQLAlchemy needed)
  - print_complete alert in _job_ended() 
  - print_failed alert in _job_ended()
  - spool_low check in _on_status() AMS data processing

Run from /opt/printfarm-scheduler/
    python3 deploy_v0170_alerts_phase4.py
"""
import os
import shutil

MONITOR_PATH = "/opt/printfarm-scheduler/backend/mqtt_monitor.py"


def backup_file(filepath):
    bak = filepath + ".bak_v017"
    if not os.path.exists(bak):
        shutil.copy2(filepath, bak)
        print(f"  Backed up {os.path.basename(filepath)}")


def patch_monitor():
    filepath = MONITOR_PATH
    backup_file(filepath)

    with open(filepath, "r") as f:
        content = f.read()

    if "_dispatch_alert" in content:
        print("  mqtt_monitor.py already has alert dispatch — skipping")
        return

    # 1. Add _dispatch_alert helper method and _last_spool_check tracker to PrinterMonitor.__init__
    content = content.replace(
        "        self._last_progress_update: float = 0\n        self._lock = Lock()",
        "        self._last_progress_update: float = 0\n        self._last_spool_check: float = 0\n        self._lock = Lock()"
    )

    # 2. Add _dispatch_alert method after disconnect()
    dispatch_method = '''
    def _dispatch_alert(self, alert_type: str, severity: str, title: str, 
                        message: str = "", job_id: int = None, spool_id: int = None,
                        metadata: dict = None):
        """
        Create alert records for all users who have this alert type enabled.
        Uses raw SQL to avoid importing SQLAlchemy into the monitor daemon.
        Handles deduplication for spool_low alerts.
        """
        try:
            conn = sqlite3.connect(DB_PATH)
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
                        WHERE user_id = ? AND alert_type = 'spool_low' 
                        AND spool_id = ? AND is_read = 0 AND is_dismissed = 0
                        LIMIT 1
                    """, (user_id, spool_id))
                    if cur.fetchone():
                        continue
                
                # Create in-app alert
                cur.execute("""
                    INSERT INTO alerts 
                    (user_id, alert_type, severity, title, message, 
                     printer_id, job_id, spool_id, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, alert_type, severity, title, message,
                      self.printer_id, job_id, spool_id,
                      json.dumps(metadata) if metadata else None,
                      datetime.now().isoformat()))
                created += 1
            
            conn.commit()
            conn.close()
            if created > 0:
                log.info(f"[{self.name}] Alert dispatched: {alert_type} to {created} users")
        except Exception as e:
            log.error(f"[{self.name}] Failed to dispatch alert: {e}")

'''

    content = content.replace(
        "    def _on_status(self, status):",
        dispatch_method + "    def _on_status(self, status):"
    )

    # 3. Add spool low check in _on_status (after progress update, before state transition check)
    spool_check_code = '''
            # Check AMS spool levels (throttled to every 60 seconds)
            if time.time() - self._last_spool_check >= 60:
                self._check_spool_levels()
                self._last_spool_check = time.time()
            
'''

    content = content.replace(
        "            # Check for state transitions\n            gcode_state = self._state.get('gcode_state')",
        spool_check_code + "            # Check for state transitions\n            gcode_state = self._state.get('gcode_state')"
    )

    # 4. Add _check_spool_levels method before _on_state_change
    spool_method = '''
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

'''

    content = content.replace(
        "    def _on_state_change(self, old_state: Optional[str], new_state: str):",
        spool_method + "    def _on_state_change(self, old_state: Optional[str], new_state: str):"
    )

    # 5. Add alert dispatch calls in _job_ended, after the existing conn.commit()/close()
    # Replace the end of _job_ended to add alert dispatch
    old_job_ended_tail = """            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job {status}: DB id {self._current_job_id}")
            self._current_job_id = None
            self._linked_job_id = None
        except Exception as e:
            log.error(f"[{self.name}] Failed to record job end: {e}")"""

    new_job_ended_tail = """            conn.commit()
            conn.close()
            log.info(f"[{self.name}] Job {status}: DB id {self._current_job_id}")
            
            # Dispatch alerts for job completion/failure
            job_name = self._state.get('subtask_name', 'Unknown')
            if status == 'completed':
                self._dispatch_alert(
                    alert_type='print_complete',
                    severity='info',
                    title=f"Print Complete: {job_name} ({self.name})",
                    message=f"Job finished successfully on {self.name}.",
                    job_id=self._linked_job_id or self._current_job_id,
                )
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
                    job_id=self._linked_job_id or self._current_job_id,
                    metadata={"progress_percent": progress, "error_code": err}
                )
            
            self._current_job_id = None
            self._linked_job_id = None
        except Exception as e:
            log.error(f"[{self.name}] Failed to record job end: {e}")"""

    content = content.replace(old_job_ended_tail, new_job_ended_tail)

    with open(filepath, "w") as f:
        f.write(content)

    print("  mqtt_monitor.py patched with alert triggers")


def main():
    print("=" * 60)
    print("v0.17.0 Phase 4 — Wire Alert Triggers")
    print("=" * 60)
    print()

    print("[1/1] Patching mqtt_monitor.py...")
    patch_monitor()

    print()
    print("=" * 60)
    print("Done! Restart the monitor daemon:")
    print("  systemctl restart printfarm-monitor")
    print()
    print("Triggers wired:")
    print("  - print_complete: fires when MQTT reports FINISH")
    print("  - print_failed: fires when MQTT reports FAILED (with progress % and error code)")
    print("  - spool_low: checks spool weights every 60s, fires when < threshold")
    print()
    print("All triggers use raw SQL (no SQLAlchemy import needed).")
    print("Dedup: spool_low skips if unread alert exists for same spool.")
    print("=" * 60)


if __name__ == "__main__":
    main()
