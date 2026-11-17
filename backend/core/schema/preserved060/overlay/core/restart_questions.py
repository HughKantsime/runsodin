"""Operator decisions for unbound Bambu observations; never dispatch authority.

The caller owns the transaction. A question and its alerts share that transaction.
No elapsed time, progress or printer identifier is used to infer physical identity.
"""
import json
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView
from core.db_compat import as_utc_datetime
from core.interfaces.education_policy import is_education_reserved_filename
from core.physical_print import CLOSED_UNKNOWN


ENDED_UNKNOWN = 'closed: ended while unconfirmed; outcome and end time unknown'


def now():
    return datetime.now(timezone.utc).isoformat()


def load(db, key):
    row = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).first()
    return (json.loads(row[0]) if isinstance(row[0], str) else row[0]) if row else None


def save(db, key, value):
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
               {'key': key, 'value': json.dumps(value)})


def begin_transaction(executor):
    """Open the caller-owned transaction before taking restart-question locks."""
    db = TransactionView(executor)
    db.execute(text('BEGIN IMMEDIATE' if db.dialect.name == 'sqlite' else 'BEGIN'))


@contextmanager
def locked(executor):
    """Serialize both processes, including absent question keys, without a new table.

    The existing membership fence protects receipt/Education exclusions. NOWAIT
    table locks also fence concurrent legacy terminal writers on PostgreSQL; a
    busy database refuses the observation/answer, never falls through to INSERT.
    Callers use begin_transaction before entering here.
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
    claim = db.execute(text('SELECT job_id,owner_org_id,token FROM printer_dispatch_claims WHERE printer_id=:id'), {'id': printer_id}).mappings().first()
    return printer, claim


def read_row(db, identifier):
    return db.execute(text('SELECT id,printer_id,scheduled_job_id,status,started_at,filename,job_name FROM print_jobs WHERE id=:id'), {'id': identifier}).mappings().first()


def claim_key(claim):
    return hashlib.sha256(json.dumps(dict(claim), sort_keys=True).encode()).hexdigest()


def check_question(db, question, printer, claim):
    row = read_row(db, question['print_job_id'])
    if (not printer or not claim or printer['api_type'] != 'bambu'
            or question.get('claim_key') != claim_key(claim)
            or question['org_id'] != printer['org_id'] or claim['owner_org_id'] != printer['org_id']
            or not eligible(db, row, claim, printer['org_id']) or row['printer_id'] != printer['id']
            or str(row['started_at']) != question['row_started_at'] or row['scheduled_job_id'] != question['scheduled_job_id']):
        raise ValueError('Restart question ownership changed')
    return row


def resolve_alerts(db, question):
    from modules.notifications.services import resolve_restart_alerts
    resolve_restart_alerts(db, question['alert_ids'])


def clear(db, printer_id):
    db.execute(text('DELETE FROM system_config WHERE key=:key'), {'key': f'restart_question:{printer_id}'})


def retire(db, printer_id, question, reason):
    """Retirement and its explanation commit once with alert resolution."""
    resolve_alerts(db, question)
    from modules.notifications.services import stage_restart_alerts
    stage_restart_alerts(db, printer_id, '', dict(question, scheduled_job_id=None, title='Print confirmation closed', message=reason))
    clear(db, printer_id)


def fingerprint(db, printer_id):
    """One read detects decisions and changes to every protected row's authority.

    Unchanged packets need no locks, membership fences, writes or alert work.
    Progress is deliberately absent: it cannot establish print identity.
    """
    return [tuple(row) for row in db.execute(text("""
        SELECT q.value,p.org_id,p.api_type,c.job_id,c.owner_org_id,c.token,
               r.id,r.scheduled_job_id,r.started_at,r.filename,r.job_name,
               j.printer_id,j.charged_to_org_id,
               EXISTS(SELECT 1 FROM dispatch_attempt_receipts d WHERE d.observed_print_job_id=r.id),
               EXISTS(SELECT 1 FROM education_submissions e WHERE e.job_id=r.scheduled_job_id)
        FROM printers p
        LEFT JOIN system_config q ON q.key='restart_question:' || CAST(p.id AS TEXT)
        LEFT JOIN printer_dispatch_claims c ON c.printer_id=p.id
        LEFT JOIN print_jobs r ON r.printer_id=p.id AND r.status='running'
        LEFT JOIN jobs j ON j.id=r.scheduled_job_id
        WHERE p.id=:id ORDER BY r.id
    """), {'id': printer_id}).all()]


def observe(executor, printer_id, started_at, state):
    """Re-evaluate after every retirement before granting the ordinary path."""
    with locked(executor) as db:
        key = f'restart_question:{printer_id}'
        question = load(db, key)
        printer, claim = context(db, printer_id)
        gstate = state.get('gcode_state')
        name = state.get('subtask_name') or state.get('gcode_file')
        epoch = started_at.isoformat()
        previous_ids = set()
        if question:
            previous_ids.update(question.get('candidate_ids', [question['print_job_id']]))
            if question.get('active_print_job_id'):
                previous_ids.add(question['active_print_job_id'])
        if question and question['state'] in ('same', 'new'):
            # Deliver an accepted answer once, even across a process restart.
            # Once recording resumed, a later restart needs a fresh decision.
            current = dict(question, print_job_id=question['active_print_job_id'],
                           row_started_at=question['active_row_started_at'],
                           scheduled_job_id=question['active_scheduled_job_id'])
            try:
                row = check_question(db, current, printer, claim)
            except ValueError:
                retire(db, printer_id, question, 'The print record or its ownership changed. Review the current records in Dispatch Recovery.')
                question = None
            else:
                if question.get('applied_monitor_started_at') not in (None, epoch):
                    retire(db, printer_id, question, 'ODIN restarted after recording resumed. Confirm the current print again; the earlier answer was already used.')
                elif not name or name == 'Unknown' or name in (row['job_name'], row['filename']):
                    if not question.get('applied_monitor_started_at'):
                        question['applied_monitor_started_at'] = epoch
                        save(db, key, question)
                    return dict(question, adopt=row['id'], linked_job_id=row['scheduled_job_id'])
                else:
                    retire(db, printer_id, question, 'The printer reported a different print name. The earlier record is unchanged.')
                question = None
        if question and question['state'] == 'pending':
            try:
                row = check_question(db, question, printer, claim)
            except ValueError:
                retire(db, printer_id, question, 'The print record or its ownership changed. Review the current records in Dispatch Recovery.')
                question = None
            else:
                if name and name != 'Unknown' and name not in (row['job_name'], row['filename']):
                    retire(db, printer_id, question, 'The printer reported a different print name. The earlier record is unchanged.')
                    question = None
                elif gstate in ('FINISH', 'FAILED', 'IDLE'):
                    return end_unanswered(db, printer_id, question, row, state)
                else:
                    if question['monitor_started_at'] != epoch or (name and name != question['reported_name']):
                        question['monitor_started_at'] = epoch
                        if name:
                            question['reported_name'] = name
                        save(db, key, question)
                    return question
        # A changed timestamp/link must not make a formerly protected row vanish
        # from consideration. Reassigned/dispatch-owned rows remain blocked for
        # investigation; this feature cannot repair their authority.
        if not printer or printer['api_type'] != 'bambu' or not claim or claim['owner_org_id'] != printer['org_id'] or reserved_name(name):
            if question:
                retire(db, printer_id, question, 'The print records or their ownership changed. Review the current records in Dispatch Recovery.')
            return None
        rows = db.execute(text("SELECT id,printer_id,scheduled_job_id,status,started_at,filename,job_name FROM print_jobs "
                               "WHERE printer_id=:id AND status='running' ORDER BY id"), {'id': printer_id}).mappings().all()
        candidates = [row for row in rows
                      if (row['id'] in previous_ids or (row['started_at'] and as_utc_datetime(row['started_at']) < started_at))
                      and (row['id'] in previous_ids or eligible(db, row, claim, printer['org_id']))
                      and (not name or name == 'Unknown' or name in (row['job_name'], row['filename']))]
        signature = [dict(row) for row in candidates]
        # JSON normalizes DB datetime values the same way on both backends.
        signature = json.loads(json.dumps(signature, default=str))
        answerable = len(candidates) == 1 and eligible(db, candidates[0], claim, printer['org_id'])
        if question:
            if (not answerable and question['state'] == 'investigation' and question.get('candidates') == signature
                    and question.get('claim_key') == claim_key(claim) and question['org_id'] == printer['org_id']):
                return question
            retire(db, printer_id, question, 'The print records or their ownership changed. Review the current records in Dispatch Recovery.')
        if not candidates or (not previous_ids and gstate not in ('RUNNING', 'PAUSE')):
            return None
        row = candidates[0]
        question = dict(id=uuid4().hex, state='pending', claim_key=claim_key(claim), print_job_id=row['id'], org_id=printer['org_id'],
                        scheduled_job_id=row['scheduled_job_id'], row_started_at=str(row['started_at']),
                        raised_at=now(), monitor_started_at=epoch, reported_name=name, print_name=row['job_name'] or row['filename'] or 'print')
        if not answerable:
            ids = [r['id'] for r in candidates]
            question.update(state='investigation', candidate_ids=ids, candidates=signature,
                            title='Print records need investigation',
                            message=f'{printer["name"]}: review print records {", ".join(map(str, ids))} in print history. '
                                    'An administrator must close records that are no longer running and check their linked jobs and printer assignments. '
                                    'When one eligible running record remains, ODIN will ask whether it is the same print. The retained dispatch stays blocked.')
        from modules.notifications.services import stage_restart_alerts
        question['alert_ids'] = stage_restart_alerts(db, printer_id, printer['name'],
                                                   dict(question, scheduled_job_id=None) if not answerable else question)
        save(db, key, question)
        if answerable and gstate in ('FINISH', 'FAILED', 'IDLE'):
            return end_unanswered(db, printer_id, question, row, state)
        return question


def end_unanswered(db, printer_id, question, row, state):
    gstate, error = state.get('gcode_state'), state.get('print_error')
    unknown = gstate == 'IDLE'
    outcome = ('completed' if gstate == 'FINISH' else 'cancelled' if unknown or error in
               (0x0300400C, 0x0500400E, str(0x0300400C), str(0x0500400E)) else 'failed')
    ended = now()
    reason = ENDED_UNKNOWN if unknown else 'Ended while unconfirmed; duration and filament use are unverified.'
    db.execute(text("UPDATE print_jobs SET status=:status,ended_at=:ended,error_code=:reason,"
                    'progress_percent=COALESCE(:progress,progress_percent),'
                    'current_layer=COALESCE(:layer,current_layer),'
                    'remaining_minutes=COALESCE(:remaining,remaining_minutes) WHERE id=:id'),
               {'id': row['id'], 'status': outcome, 'ended': None if unknown else ended, 'reason': reason,
                'progress': None if unknown else state.get('mc_percent'),
                'layer': None if unknown else state.get('layer_num'),
                'remaining': None if unknown else state.get('mc_remaining_time')})
    save(db, f'restart_print:{row["id"]}', {'question_id': question['id'], 'outcome': 'ended_unconfirmed',
         'duration_verified': False, 'usage_verified': False, 'reported_result': 'unknown' if unknown else outcome, 'end_received_at': ended})
    retire(db, printer_id, question, 'The printer is idle; the print outcome and end time are unknown. The question closed unanswered. Duration and filament use are unverified.' if unknown else
           f'The printer reported the print ended ({outcome}). The question closed unanswered. Duration and filament use are unverified. The retained dispatch still needs review.')
    return dict(question, state='ended_unconfirmed', reported_result='unknown' if unknown else outcome)


def visible(db, printer_id, actor):
    from core.rbac import check_org_access
    from core.controlled_parts import allowed
    view = TransactionView(db)
    question = load(view, f'restart_question:{printer_id}')
    if not question or question['state'] not in ('pending', 'investigation'):
        return None
    printer, claim = context(view, printer_id)
    if (not printer or question['org_id'] != printer['org_id']
            or not check_org_access(actor, printer['org_id'])
            or not allowed(db, 'printer', printer_id, actor)):
        return None
    if question['state'] == 'pending':
        try:
            check_question(view, question, printer, claim)
        except ValueError:
            return None
    for identifier in question.get('candidate_ids', [question['print_job_id']]):
        row = read_row(view, identifier)
        if (not row or row['printer_id'] != printer_id
                or any(reserved_name(row[k]) for k in ('filename', 'job_name'))
                or not allowed(db, 'print_job', identifier, actor)):
            return None
        if row['scheduled_job_id'] is not None:
            owner = db.execute(text('SELECT charged_to_org_id FROM jobs WHERE id=:id'), {'id': row['scheduled_job_id']}).first()
            if (not owner or not check_org_access(actor, owner[0])
                    or db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'), {'id': row['scheduled_job_id']}).first()):
                return None
    return {k: question[k] for k in ('id', 'state', 'print_job_id', 'raised_at', 'print_name', 'reported_name', 'candidate_ids', 'message') if k in question}


def answer(db, printer_id, actor, question_id, decision):
    from fastapi import HTTPException
    from core.audit import log_audit
    from core.rbac import check_org_access
    try:
        db.rollback()
        begin_transaction(db)
        with locked(db) as view:
            question = load(view, f'restart_question:{printer_id}')
            if not question:
                raise HTTPException(409, 'This question has closed. Refresh to see the current records.')
            if not check_org_access(actor, question['org_id']):
                raise HTTPException(404, 'Print question not found.')
            if question['id'] != question_id or question['state'] != 'pending':
                raise HTTPException(409, 'This question has already changed. Refresh to see its current state.')
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only the fixed FOR UPDATE NOWAIT suffix is joined, chosen by dialect; the id is bound
            live = db.execute(text('SELECT role,group_id,is_active FROM users WHERE id=:id' +
                                   (' FOR UPDATE NOWAIT' if view.dialect.name == 'postgresql' else '')), {'id': actor['id']}).first()
            if not live or live[0] not in ('admin', 'operator') or live[1] != actor.get('group_id') or not live[2]:
                raise HTTPException(403, 'An authorized operator or administrator must answer this question.')
            # Print confirmation uses printer and print-row visibility. It does
            # not grant authority over the separately retained dispatch claim.
            if visible(db, printer_id, actor) is None:
                raise HTTPException(404, 'Print question not found.')
            printer, claim = context(view, printer_id)
            row = check_question(view, question, printer, claim)
            answered_at = now()
            active_id = row['id']
            if decision == 'new':
                from core.monitor_observations import live_state
                if live_state(db, printer_id, 0) not in ('RUNNING', 'PAUSE'):
                    raise HTTPException(409, 'Wait for a fresh running report from the printer before choosing a new print.')
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
