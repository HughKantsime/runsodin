"""Operator decisions for unbound Bambu observations; never dispatch authority.

The caller owns the transaction. A question and its alerts share that transaction.
No elapsed time, progress or printer identifier is used to infer physical identity.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView
from core.db_compat import as_utc_datetime
from core.interfaces.education_policy import is_education_reserved_filename
from core.physical_print import CLOSED_UNKNOWN


def now():
    return datetime.now(timezone.utc).isoformat()


def load(db, key):
    row = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).first()
    return (json.loads(row[0]) if isinstance(row[0], str) else row[0]) if row else None


def save(db, key, value):
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
               {'key': key, 'value': json.dumps(value)})


@contextmanager
def locked(executor):
    """Serialize both processes, including absent question keys, without a new table.

    The existing membership fence protects receipt/Education exclusions. NOWAIT
    table locks also fence concurrent legacy terminal writers on PostgreSQL; a
    busy database refuses the observation/answer, never falls through to INSERT.
    SQLite callers begin IMMEDIATE before entering here.
    """
    from core.attempt_start_transaction import start_transaction
    with start_transaction(executor) as scope:
        scope.fence_dispatch_membership(executor, education=True)
        db = TransactionView(executor)
        if db.dialect.name == 'postgresql':
            db.execute(text('LOCK TABLE printers, print_jobs, system_config IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
        yield db


def reserved_name(name):
    return is_education_reserved_filename(name) or (isinstance(name, str) and 'odin-dispatch-' in name.lower())


def eligible(db, row, claim, org_id):
    if row is None or row['status'] != 'running' or any(reserved_name(row[k]) for k in ('filename', 'job_name')):
        return False
    if db.execute(text('SELECT 1 FROM dispatch_attempt_receipts WHERE observed_print_job_id=:id'), {'id': row['id']}).first():
        return False
    job = row['scheduled_job_id']
    if job is not None:
        if job == claim['job_id'] or db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'), {'id': job}).first():
            return False
        owner = db.execute(text('SELECT charged_to_org_id,printer_id FROM jobs WHERE id=:id'), {'id': job}).first()
        if owner is None or tuple(owner) != (org_id, row['printer_id']):
            return False
    return True


def context(db, printer_id):
    printer = db.execute(text('SELECT id,name,org_id,api_type FROM printers WHERE id=:id'), {'id': printer_id}).mappings().first()
    claim = db.execute(text('SELECT job_id,owner_org_id FROM printer_dispatch_claims WHERE printer_id=:id'), {'id': printer_id}).mappings().first()
    return printer, claim


def read_row(db, identifier):
    return db.execute(text('SELECT id,printer_id,scheduled_job_id,status,started_at,filename,job_name FROM print_jobs WHERE id=:id'), {'id': identifier}).mappings().first()


def check_question(db, question, printer, claim):
    row = read_row(db, question['print_job_id'])
    if (not printer or not claim or printer['api_type'] != 'bambu'
            or question['org_id'] != printer['org_id'] or claim['owner_org_id'] != printer['org_id']
            or not eligible(db, row, claim, printer['org_id']) or row['printer_id'] != printer['id']
            or str(row['started_at']) != question['row_started_at'] or row['scheduled_job_id'] != question['scheduled_job_id']):
        raise ValueError('Restart question ownership changed')
    return row


def resolve_alerts(db, question):
    from modules.notifications.services import resolve_restart_alerts
    resolve_restart_alerts(db, question['alert_ids'])


def observe(executor, printer_id, started_at, state):
    """Return a pending/closed question or an explicit row adoption, never a guess."""
    with locked(executor) as db:
        key = f'restart_question:{printer_id}'
        question = load(db, key)
        printer, claim = context(db, printer_id)
        gstate = state.get('gcode_state')
        epoch = started_at.isoformat()
        # An answer concerns the run that observed the question. It is never
        # evidence of identity after another process restart.
        if question and question['state'] in ('same', 'new'):
            reported = question['reported_name']
            incoming = state.get('subtask_name')
            if question['monitor_started_at'] != epoch or (reported and incoming and reported != 'Unknown' and incoming != reported):
                question['state'] = 'superseded'
                save(db, key, question)
                question = None
        if question and question['state'] in ('same', 'new'):
            row = read_row(db, question['active_print_job_id'])
            if row and row['status'] == 'running':
                # An answered question remains a durable instruction across a
                # second monitor restart, but cannot override changed authority.
                current = dict(question, print_job_id=question['active_print_job_id'],
                               row_started_at=question['active_row_started_at'],
                               scheduled_job_id=question['active_scheduled_job_id'])
                check_question(db, current, printer, claim)
                return dict(question, adopt=row['id'], linked_job_id=row['scheduled_job_id'])
            question['state'] = 'ended'
            save(db, key, question)
        if question and question['state'] == 'pending':
            row = check_question(db, question, printer, claim)
            if gstate in ('FINISH', 'FAILED', 'IDLE'):
                error = state.get('print_error')
                outcome = ('completed' if gstate == 'FINISH' else 'cancelled' if gstate == 'IDLE' or error in
                           (0x0300400C, 0x0500400E, str(0x0300400C), str(0x0500400E)) else 'failed')
                ended = now()
                # Physical bookkeeping only. No job transition, claim release,
                # terminal accounting or success event for unconfirmed identity.
                db.execute(text("UPDATE print_jobs SET status=:status,ended_at=:ended,error_code=:reason,"
                                'progress_percent=COALESCE(:progress,progress_percent),'
                                'current_layer=COALESCE(:layer,current_layer),'
                                'remaining_minutes=COALESCE(:remaining,remaining_minutes) WHERE id=:id'),
                           {'id': row['id'], 'status': outcome, 'ended': ended,
                            'reason': 'Ended while unconfirmed; duration and filament use are unverified.',
                            'progress': state.get('mc_percent'), 'layer': state.get('layer_num'),
                            'remaining': state.get('mc_remaining_time')})
                save(db, f'restart_print:{row["id"]}', {'question_id': question['id'], 'outcome': 'ended_unconfirmed',
                     'duration_verified': False, 'usage_verified': False, 'reported_result': outcome, 'end_received_at': ended})
                question.update(state='ended_unconfirmed', ended_at=ended, reported_result=outcome)
                resolve_alerts(db, question)
                save(db, key, question)
                return question
            name = state.get('subtask_name')
            if question['monitor_started_at'] != epoch or (name and name != question['reported_name']):
                question['monitor_started_at'] = epoch
                if name:
                    question['reported_name'] = name
                save(db, key, question)
            return question
        if gstate not in ('RUNNING', 'PAUSE'):
            return question if question and question['state'] in ('ended', 'ended_unconfirmed') else None
        name = state.get('subtask_name')
        if not printer or printer['api_type'] != 'bambu' or not claim or claim['owner_org_id'] != printer['org_id'] or reserved_name(name):
            return None
        rows = db.execute(text("SELECT id,printer_id,scheduled_job_id,status,started_at,filename,job_name FROM print_jobs "
                               "WHERE printer_id=:id AND status='running' ORDER BY id"), {'id': printer_id}).mappings().all()
        candidates = [row for row in rows if eligible(db, row, claim, printer['org_id'])
                      and row['started_at'] and as_utc_datetime(row['started_at']) < started_at
                      and (not name or name == 'Unknown' or name in (row['job_name'], row['filename']))]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError('Multiple earlier prints require investigation')
        row = candidates[0]
        question = dict(id=uuid4().hex, state='pending', print_job_id=row['id'], org_id=printer['org_id'],
                        scheduled_job_id=row['scheduled_job_id'], row_started_at=str(row['started_at']),
                        raised_at=now(), monitor_started_at=epoch, reported_name=name, print_name=row['job_name'] or row['filename'] or 'print')
        from modules.notifications.services import stage_restart_alerts
        question['alert_ids'] = stage_restart_alerts(db, printer_id, printer['name'], question)
        save(db, key, question)
        return question


def visible(db, printer_id, actor):
    from core.rbac import check_org_access
    from core.controlled_parts import allowed
    view = TransactionView(db)
    question = load(view, f'restart_question:{printer_id}')
    if not question or question['state'] != 'pending':
        return None
    printer, claim = context(view, printer_id)
    try:
        check_question(view, question, printer, claim)
    except ValueError:
        return None
    if not check_org_access(actor, question['org_id']) or not allowed(db, 'print_job', question['print_job_id'], actor):
        return None
    return {k: question[k] for k in ('id', 'print_job_id', 'raised_at', 'print_name', 'reported_name')}


def answer(db, printer_id, actor, question_id, decision):
    from fastapi import HTTPException
    from core.audit import log_audit
    from core.rbac import check_org_access
    from core.controlled_parts import allowed
    try:
        db.rollback()
        if db.get_bind().dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        with locked(db) as view:
            question = load(view, f'restart_question:{printer_id}')
            if not question or not check_org_access(actor, question['org_id']):
                raise HTTPException(404, 'Print question not found.')
            if question['id'] != question_id or question['state'] != 'pending':
                raise HTTPException(409, 'This question has already changed. Refresh to see its current state.')
            live = db.execute(text('SELECT role,group_id,is_active FROM users WHERE id=:id' +
                                   (' FOR UPDATE NOWAIT' if view.dialect.name == 'postgresql' else '')), {'id': actor['id']}).first()
            if not live or live[0] != 'admin' or live[1] != actor.get('group_id') or not live[2]:
                raise HTTPException(403, 'An authorized administrator must answer this question.')
            from core.dispatch_recovery import capture, authorized
            evidence = capture(db, printer_id, actor)
            if evidence is None:
                raise HTTPException(404, 'Print question not found.')
            authorized(db, evidence, actor)
            printer, claim = context(view, printer_id)
            row = check_question(view, question, printer, claim)
            if not allowed(db, 'printer', printer_id, actor) or not allowed(db, 'print_job', row['id'], actor):
                raise HTTPException(404, 'Print question not found.')
            answered_at = now()
            active_id = row['id']
            if decision == 'new':
                name = question['reported_name'] or 'Unknown'
                if reserved_name(name):
                    raise HTTPException(409, 'The reported print has changed. Refresh and investigate before answering.')
                from core.attempt_start_observation import observe_start
                from core.order_production import prepare_print_job_write
                prepare_print_job_write(db, row['id'])
                view.execute(text("UPDATE print_jobs SET status='cancelled',ended_at=NULL,error_code=:reason WHERE id=:id"),
                             {'id': row['id'], 'reason': CLOSED_UNKNOWN})
                created = observe_start(db.connection(), printer_id=printer_id, protocol='bambu', filename='', job_name=name,
                                        physical_values={'started_at': answered_at}, source='bambu-operator-first-seen-mid-print')
                if not created.fresh or created.linked_job_id is not None:
                    raise ValueError('Operator decision cannot bind dispatch authority')
                active_id = created.id
                save(view, f'restart_print:{active_id}', {'question_id': question_id, 'first_seen_mid_print': True,
                     'duration_verified': False, 'usage_verified': False, 'first_seen_at': answered_at})
            active = read_row(view, active_id)
            question.update(state=decision, active_print_job_id=active_id, answered_at=answered_at, actor_id=actor['id'],
                            active_row_started_at=str(active['started_at']), active_scheduled_job_id=active['scheduled_job_id'])
            resolve_alerts(view, question)
            save(view, f'restart_question:{printer_id}', question)
            log_audit(db, 'restart_question_answered', 'printer', printer_id,
                      {'question_id': question_id, 'print_job_id': row['id'], 'active_print_job_id': active_id,
                       'answer': decision, 'actor_id': actor['id'], 'answered_at': answered_at})
            db.flush()
        db.commit()
        return {'outcome': decision, 'print_job_id': active_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(409, 'The print question could not be updated. Refresh and try again.') from None
