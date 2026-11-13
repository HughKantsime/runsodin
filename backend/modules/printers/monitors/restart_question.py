"""Bambu bridge to durable operator decisions; no packet work for ordinary printers."""


def initialize(monitor, runtime):
    # Restart eligibility is a startup fact. Scan once before callbacks can run;
    # a later dispatch cannot create a row predating this monitor process.
    monitor._restart_check_needed = True  # unreadable storage must fail closed
    monitor._restart_retry_at = 0.0
    monitor._restart_blocks_rows = False
    monitor._restart_snapshot = None
    monitor._restart_observation = None
    monitor._restart_idle_investigation = False
    try:
        from core.dispatch_observation_executor import TransactionView
        from core.restart_questions import load, context, eligible
        from core.db_compat import as_utc_datetime
        from sqlalchemy import text
        with runtime.get_db() as conn:
            db = TransactionView(conn)
            if load(db, f'restart_question:{monitor.printer_id}'):
                return
            printer, claim = context(db, monitor.printer_id)
            if not printer or not claim or claim['owner_org_id'] != printer['org_id']:
                monitor._restart_check_needed = False
                return
            rows = db.execute(text("SELECT id,printer_id,scheduled_job_id,status,started_at,filename,job_name FROM print_jobs WHERE printer_id=:id AND status='running'"), {'id': monitor.printer_id}).mappings().all()
            monitor._restart_check_needed = any(
                row['started_at'] and as_utc_datetime(row['started_at']) < monitor._monitor_started_at
                and eligible(db, row, claim, printer['org_id']) for row in rows)
    except Exception:
        runtime.log.error('[%s] Print confirmation startup check unavailable', monitor.name)


def handle(monitor, runtime):
    if not getattr(monitor, '_restart_check_needed', False):
        return False
    if monitor._current_job_id:
        return False
    if runtime.time.time() < monitor._restart_retry_at:
        return True
    observation = (monitor._state.get('gcode_state'), monitor._state.get('subtask_name') or monitor._state.get('gcode_file'))
    if monitor._restart_idle_investigation and observation[0] == 'IDLE':
        return True
    monitor._restart_idle_investigation = False
    try:
        from core.restart_questions import observe, fingerprint
        from core.dispatch_observation_executor import TransactionView
        with runtime.get_db() as conn:
            if monitor._restart_snapshot is not None and observation == monitor._restart_observation:
                if fingerprint(TransactionView(conn), monitor.printer_id) == monitor._restart_snapshot:
                    return True
            conn.execute('BEGIN IMMEDIATE')
            question = observe(conn, monitor.printer_id, monitor._monitor_started_at, monitor._state)
            if question and question['state'] in ('pending', 'investigation'):
                monitor._restart_snapshot = fingerprint(TransactionView(conn), monitor.printer_id)
                monitor._restart_observation = observation
                monitor._restart_idle_investigation = question['state'] == 'investigation' and observation[0] == 'IDLE'
            else:
                monitor._restart_snapshot = None
            conn.commit()
            monitor._restart_retry_at = 0.0
            if question and question.get('adopt'):
                from modules.printers.monitors.mqtt_job_lifecycle import load_mqtt_start_weights
                monitor._current_job_id = question['adopt']
                monitor._linked_job_id = question['linked_job_id']
                monitor._start_spool_weights = load_mqtt_start_weights(conn, question['adopt'])
                monitor._reconciled_row = question['adopt']
                monitor._pending_running_state = None
                if monitor._last_gcode_state is None:
                    monitor._last_gcode_state = 'RUNNING'
                return False
        if not question or question['state'] == 'ended_unconfirmed':
            monitor._restart_check_needed = False
            monitor._recovery_pending = False
            monitor._pending_running_state = None
            if not question:
                monitor._last_gcode_state = None
            if question and question['reported_result'] != 'unknown':
                from modules.archives.services import create_print_archive
                create_print_archive(question['print_job_id'], monitor.printer_id,
                                     question['reported_result'] == 'completed')
            return bool(question)  # withhold row writers for this end packet only
        monitor._pending_running_state = None
        return True
    except Exception:
        # This is independent of ordinary orphan recovery's backoff. Failure
        # never grants permission to insert, and never suppresses HMS/display.
        runtime.log.error('[%s] Print confirmation check unavailable', monitor.name)
        monitor._restart_retry_at = runtime.time.time() + 10
        return True


def clear_finished(monitor, runtime):
    """Consume an answered instruction once the normal terminal writer commits."""
    if not getattr(monitor, '_restart_check_needed', False):
        return
    from core.restart_questions import load, clear, locked, read_row
    with runtime.get_db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        with locked(conn) as db:
            question = load(db, f'restart_question:{monitor.printer_id}')
            if question and question['state'] in ('same', 'new'):
                row = read_row(db, question['active_print_job_id'])
                if row and row['status'] == 'running':
                    return
                clear(db, monitor.printer_id)
        conn.commit()
    monitor._restart_check_needed = False
