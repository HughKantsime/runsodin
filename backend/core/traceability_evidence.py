"""Retained per-attempt evidence; no credentials or guessed live tray assignments."""
from core.print_attempts import bound_print_attempt, sqlalchemy_runner, moment

LIFECYCLE = frozenset(('job.created', 'job.submitted', 'job.dispatched', 'job.dispatch_requested',
                       'job.started', 'job.completed', 'job.failed', 'job.cancelled'))


def attempt_evidence(db, job, seq, reset, *, strict=False):
    from core.traceability import rows, decoded
    run = sqlalchemy_runner(db)
    physical = [p for p in rows(db, 'SELECT id,spool_snapshot FROM print_jobs WHERE scheduled_job_id=:id ORDER BY id', id=job['id'])
                if bound_print_attempt(run, p['id'], job['id']) == seq]
    material_evidence = []
    for p in physical:
        snapshot = decoded(p['spool_snapshot'], {}) or {}
        plan = snapshot.get('material_plan') or {} if isinstance(snapshot, dict) else None
        valid = (isinstance(snapshot, dict) and isinstance(plan, dict) and
                 isinstance(plan.get('allocations', []), list) and isinstance(snapshot.get('slots', []), list))
        valid = valid and all(isinstance(value, dict) for value in plan.get('allocations', []) + snapshot.get('slots', []))
        if not valid:
            if strict:
                raise ValueError('Malformed attempt material snapshot')
            # A deferred/legacy capture must remain readable without inventing
            # manufacturing facts from a malformed retained spool snapshot.
            material_evidence.append({'print_job_id':p['id'], 'state':'unavailable',
                'reason':'malformed_spool_snapshot', 'allocations':[], 'spools':[], 'slots_at_start':[]})
            continue
        # Whole start snapshots also carry financial/ownership data and unused
        # foreign trays. Export only manufacturing evidence for this owner.
        plan = snapshot.get('material_plan') or {}
        permitted = (None, job['charged_to_org_id'])
        allocations = [a for a in plan.get('allocations', []) if a.get('spool_org_id') in permitted]
        spools = []
        for spool_id in sorted({a['spool_id'] for a in allocations if a.get('spool_id') is not None}):
            spools += rows(db, '''SELECT s.id AS spool_id,f.material,s.color_name,s.color_hex,s.lot_number,s.vendor,f.brand
                FROM spools s LEFT JOIN filament_library f ON f.id=s.filament_id
                WHERE s.id=:id AND (s.org_id IS NULL OR s.org_id=:org)''', id=spool_id, org=job['charged_to_org_id'])
        material_evidence.append({'print_job_id':p['id'], 'state':plan.get('state'), 'reason':plan.get('reason'),
            'allocations':allocations, 'spools':spools,
            'slots_at_start':[s for s in snapshot.get('slots', []) if s.get('spool_org_id') in permitted]})
    dispatched_hash = None
    receipts = []
    for p in physical:
        # The receipt identifies the physical attempt; the retained sender carries
        # the admitted payload digest (the receipt itself has no hash column).
        receipts += rows(db, '''SELECT r.claim_token,s.payload_sha256 FROM dispatch_attempt_receipts r
            LEFT JOIN dispatch_sender_registrations s ON s.claim_token=r.claim_token AND s.job_id=r.job_id AND s.printer_id=r.printer_id
            WHERE r.job_id=:job AND r.observed_print_job_id=:id''', job=job['id'], id=p['id'])
    if receipts:
        dispatched_hash = receipts[-1].get('payload_sha256')
    events = []
    operators = set()
    if seq == 0 and job.get('submitted_by') is not None:
        operators.add(job['submitted_by'])
    for a in rows(db, "SELECT id,timestamp,action,actor_user_id,details FROM audit_logs WHERE entity_type='job' AND entity_id=:id ORDER BY id", id=job['id']):
        if a['action'] not in LIFECYCLE:
            continue
        details = decoded(a['details'], {})
        details = details if isinstance(details, dict) else {}
        if details.get('attempt') is not None:
            if details['attempt'] != seq:
                continue
        elif reset is not None and (moment(a['timestamp']) is None or moment(a['timestamp']) < moment(reset)):
            continue
        elif seq > 0 and reset is None:
            continue
        if a['action'] not in ('job.failed', 'job.cancelled') and a['actor_user_id'] is not None:
            operators.add(a['actor_user_id'])
        if a['action'] in ('job.dispatched', 'job.dispatch_requested') and details.get('queued_by_user_id') is not None:
            operators.add(details['queued_by_user_id'])
        events.append({k:a[k] for k in ('id', 'timestamp', 'action')})
    return {'dispatched_sha256': dispatched_hash,
            'hash_source': 'dispatch_attempt_receipts -> dispatch_sender_registrations.payload_sha256' if dispatched_hash else None,
            'operator_ids': sorted(operators), 'lifecycle': events,
            'attempt_material_evidence': material_evidence}


def capture_completion(executor, job_id):
    """Best-effort capture must never roll back a committed physical outcome."""
    import logging
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from core.dispatch_observation_executor import TransactionView
    if isinstance(executor, Session):
        executor.flush()  # The terminal transition belongs to the outer scope.
    db = TransactionView(executor)
    if db.dialect.name == 'sqlite':
        # SQLite's legacy driver does not BEGIN on SELECT. An outermost
        # SAVEPOINT would commit on RELEASE, defeating caller rollback.
        from sqlalchemy.engine import Connection
        connection = executor.connection() if isinstance(executor, Session) else executor
        raw = connection.connection if isinstance(connection, Connection) else getattr(connection, '_connection', connection)
        driver = getattr(raw, 'driver_connection', raw)
        if getattr(driver, 'in_transaction', None) is False:
            db.execute(text('BEGIN'))
    db.execute(text('SAVEPOINT traceability_capture'))
    try:
        _capture_completion(executor, job_id)
    except Exception as exc:
        db.execute(text('ROLLBACK TO SAVEPOINT traceability_capture'))
        # Do not log raw spool snapshots, credentials or statement parameters.
        logging.getLogger('odin.traceability').warning(
            'Traceability capture deferred for job %s (%s)', job_id, type(exc).__name__)
    finally:
        db.execute(text('RELEASE SAVEPOINT traceability_capture'))


def _capture_completion(executor, job_id):
    """Called inside the terminal transaction; archive accounting may arrive later.

    Older preserved schema producers and small protocol test doubles may not yet
    have E4. Feature gating controls access, not evidence retention.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session
    from core.dispatch_observation_executor import TransactionView
    if isinstance(executor, Session):
        executor.flush()
    if isinstance(executor, (Session, Connection)):
        connection = executor.connection() if isinstance(executor, Session) else executor
        if not inspect(connection).has_table('traceability_records'):
            return
    else:
        view = TransactionView(executor)
        query = ("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='traceability_records'"
                 if view.dialect.name == 'postgresql' else "SELECT name FROM sqlite_master WHERE type='table' AND name='traceability_records'")
        if view.execute(text(query)).first() is None:
            return
    db = TransactionView(executor)
    from core.traceability import one, record
    job = one(db, 'SELECT * FROM jobs WHERE id=:id', id=job_id)
    if not job or job['status'] != 'completed' or one(db, 'SELECT id FROM education_submissions WHERE job_id=:id', id=job_id):
        return
    file = one(db, 'SELECT org_id,stored_path FROM print_files WHERE id=:id', id=job['print_file_id'])
    if file:
        from core.model_preview_access import is_education_owned_file
        alias = one(db, '''SELECT original.id FROM print_files original JOIN education_submissions s
            ON s.print_file_id=original.id WHERE original.stored_path=:path AND :path IS NOT NULL LIMIT 1''', path=file['stored_path'])
        if (file['org_id'] not in (None, job['charged_to_org_id']) or alias or
                is_education_owned_file(db, job['print_file_id'], file['stored_path'])):
            # Do not preserve unauthorized facts in a record that later reads
            # correctly serve without consulting mutable source-file metadata.
            return
    record(db, job, completed_now=True)
