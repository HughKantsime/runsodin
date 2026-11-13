"""Small Bambu monitor bridge to durable operator decisions."""

def handle(monitor, runtime):
    if monitor._current_job_id:
        return False
    if runtime.time.time() < getattr(monitor, '_recovery_retry_at', 0):
        return True
    try:
        from core.restart_questions import observe
        with runtime.get_db() as conn:
            # Ordinary monitors retain their existing path and need no new locks.
            if not conn.execute('SELECT 1 FROM printer_dispatch_claims WHERE printer_id=?', (monitor.printer_id,)).fetchone() and not conn.execute('SELECT 1 FROM system_config WHERE key=?', (f'restart_question:{monitor.printer_id}',)).fetchone():
                return False
            conn.rollback()
            conn.execute('BEGIN IMMEDIATE')
            question = observe(conn, monitor.printer_id, monitor._monitor_started_at, monitor._state)
            conn.commit()
            if question:
                monitor._recovery_pending = False
                monitor._recovery_failures = 0
                monitor._recovery_retry_at = 0.0
            if question and question.get('adopt'):
                from modules.printers.monitors.mqtt_job_lifecycle import load_mqtt_start_weights
                monitor._current_job_id = question['adopt']
                monitor._linked_job_id = question['linked_job_id']
                monitor._start_spool_weights = load_mqtt_start_weights(conn, question['adopt'])
                monitor._reconciled_row = question['adopt']
                monitor._pending_running_state = None
                monitor._last_gcode_state = 'RUNNING'
                return False
        if question:
            monitor._pending_running_state = None
            return True
        return False
    except Exception:
        # A storage/authority failure must never fall through to a duplicate.
        runtime.log.error('[%s] Orphaned job recovery failed: print confirmation check unavailable', monitor.name)
        monitor._retry_recovery_later()
        return True
