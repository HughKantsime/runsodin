"""Owner-confirmed dispatch, with durable idempotency and admission attestation."""
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import uuid
from fastapi import HTTPException
from modules.printers.dispatch_handle import status_url
from sqlalchemy import text, bindparam, JSON
from sqlalchemy.orm import Session
from core.system_config_values import config_text
from core.durable_config import insert
from core.rbac import check_org_access
from core.interfaces.completion import next_suggestion

_context = ContextVar('start_next_confirmation', default=None)


def request_key(request_id, user=None):
    actor = principal(user) if user is not None else None
    scope = ('token:' + str(actor['token_id']) if actor['token_id'] is not None else 'user:' + str(actor['user_id'])) if actor else ''
    return 'start_next:' + scope + ':' + str(request_id)


def load(conn, key):
    raw = conn.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).scalar_one_or_none()
    return json.loads(config_text(raw)) if raw is not None else None


def save(conn, key, value):
    param = bindparam('value', type_=JSON) if conn.dialect.name == 'postgresql' else bindparam('value')
    conn.execute(text('UPDATE system_config SET value=:value WHERE key=:key').bindparams(param),
                 {'key': key, 'value': json.dumps(value, sort_keys=True)})


def principal(user):
    return {'user_id': user.get('id'), 'token_id': user.get('_token_id') if user.get('_auth_kind') not in ('session_jwt', 'bearer_jwt') else None, 'org_id': user.get('group_id')}


def replay(record, user, printer_id):
    if record['actor'] != principal(user) or record['printer_id'] != printer_id:
        raise HTTPException(409, 'Request id belongs to a different operation')
    return record['result']


def admission_facts(db, job_id, printer_id):
    from core.interfaces.completion import configuration_key, finished_attempt
    row = db.execute(text('SELECT id,updated_at,printer_id,payload_sha256,plate_index,model_id,print_file_id,filament_type,colors_required,ams_mapping FROM jobs WHERE id=:id'), {'id': job_id}).mappings().first()
    finished = finished_attempt(db, printer_id)
    return {'job': {k:str(v) if v is not None else None for k,v in row.items()} if row else None,
            'configuration_key': configuration_key(db, printer_id),
            'finished_attempt_uid': finished['economic_uid'] if finished else None}


def start(db, printer, user, data, *, background=False):
    # First operation is the durable idempotency lookup. Replays never consult
    # queue state, fingerprints or mutable printer facts.
    printer_id = printer.id
    key = request_key(data.request_id, user)
    prior = load(db, key)
    if prior is not None:
        return replay(prior, user, printer_id)
    if data.bed_clear_confirmed is not True:
        raise HTTPException(422, 'Confirm the bed is clear before starting')
    visible = lambda job: check_org_access(user, job.charged_to_org_id)
    suggestion = next_suggestion(db, printer, visible=visible)
    if suggestion['job_id'] != data.suggested_job_id or suggestion['fingerprint'] != data.suggestion_fingerprint:
        raise HTTPException(409, {'message': 'Suggestion changed. Review the new suggestion.', 'suggestion': suggestion})
    record = {'actor': principal(user), 'printer_id': printer_id, 'job_id': data.suggested_job_id,
              'fingerprint': data.suggestion_fingerprint, 'attestation': data.attestation or 'owner_in_app',
              'result': {'success': False, 'state': 'preparing', 'job_id': data.suggested_job_id, 'request_id': str(data.request_id)}}
    facts = admission_facts(db, data.suggested_job_id, printer_id)
    # End the read transaction before taking a write lock. This is an ordinary
    # admission intent, never a shared dispatch transaction.
    db.rollback()
    engine = db.get_bind()
    with engine.begin() as conn:
        # Serialize printer candidates on both SQLite and PostgreSQL.
        conn.execute(text('UPDATE printers SET is_active=is_active WHERE id=:id'), {'id': printer_id})
        prior = load(conn, key)
        if prior is not None:
            return replay(prior, user, printer_id)
        if admission_facts(conn, data.suggested_job_id, printer_id) != facts:
            raise HTTPException(409, 'Candidate changed before it could be claimed')
        # The guard binds the exact candidate/revision and finished attempt to a
        # single request. A crash retains the intent and cannot authorize a resend.
        guard = 'start_next_claim:' + hashlib.sha256(f'{printer_id}:{data.suggestion_fingerprint}'.encode()).hexdigest()
        if not insert(conn, guard, {'request_id': str(data.request_id)}):
            raise HTTPException(409, {'message': 'This suggestion already has a start request.', 'suggestion': suggestion})
        if not insert(conn, key, record):
            return replay(load(conn, key), user, printer_id)
        from core.order_production import prepare_job_write, reconcile_job_write
        from modules.jobs.models import Job
        with Session(bind=conn) as claim_db:
            parents = prepare_job_write(claim_db, [data.suggested_job_id], {'printer_id': printer_id})
            job = claim_db.query(Job).filter(Job.id == data.suggested_job_id).with_for_update().one_or_none()
            if job is None or getattr(job.status, 'value', job.status) != 'pending' or job.hold or job.printer_id not in (None, printer_id):
                raise HTTPException(409, 'Candidate changed before it could be claimed')
            record['assigned_by_claim'] = job.printer_id is None
            job.printer_id = printer_id
            claim_db.flush()
            from core.registry import registry
            policy = registry.get_provider('EducationPolicyProvider')
            education = claim_db.execute(text('SELECT id,lifecycle_revision FROM education_submissions WHERE job_id=:id'), {'id':job.id}).first()
            if education:
                if policy is None or not policy.advance_schedule(claim_db, submission_id=education[0], job_id=job.id, printer_id=printer_id, expected_revision=education[1]):
                    raise HTTPException(409, 'Education scheduling authority changed')
            reconcile_job_write(claim_db, parents)
        record['admission_facts'] = admission_facts(conn, data.suggested_job_id, printer_id)
        save(conn, key, record)
    if background:
        from modules.printers.dispatch_background import stage
        def operation():
            with Session(engine) as worker_db:
                return _finish(worker_db, printer_id, user, data, key, record, guard)
        def on_staged(handle):
            with engine.begin() as conn:
                current = load(conn, key)
                current['result'] = {**current['result'], **handle}
                save(conn, key, current)
        try:
            handle = stage(printer_id, data.suggested_job_id, operation=operation, on_staged=on_staged)
        except HTTPException as exc:
            if exc.status_code == 503:
                _record_result(db, printer_id, data, key, guard, {
                    **record['result'], 'success': False, 'state': 'refused', 'message': str(exc.detail)})
            raise
        return {**record['result'], **handle}
    return _finish(db, printer_id, user, data, key, record, guard)


def _finish(db, printer_id, user, data, key, record, guard):
    context = {'key': key, 'record': record, 'user': user}
    marker = _context.set(context)
    try:
        from modules.printers.dispatch import dispatch_job
        success, message = dispatch_job(printer_id, data.suggested_job_id)
        result = {'success': success, 'message': message, 'job_id': data.suggested_job_id,
                  'request_id': str(data.request_id), 'state': 'dispatched' if success else 'refused'}
    except Exception:
        success = False
        result = {'success': False, 'message': 'Dispatch outcome requires review before retrying.', 'job_id': data.suggested_job_id, 'request_id': str(data.request_id), 'state': 'uncertain'}
    finally:
        _context.reset(marker)
    return _record_result(db, printer_id, data, key, guard, result, clean_no_start=context.get('clean_no_start', False))


def confirm_no_start():
    context = _context.get()
    if context is not None:
        context['clean_no_start'] = True


def _record_result(db, printer_id, data, key, guard, result, *, clean_no_start=False):
    from core.order_production import prepare_job_write, reconcile_job_write
    from modules.jobs.models import Job
    success = result['success']
    try:
        conn = db.connection()
        current = load(conn, key)
        if not success and current.get('claim_token') and not clean_no_start:
            result['state'] = 'uncertain'
        current['result'] = result
        save(conn, key, current)
        if result['state'] == 'refused':
            if current.get('assigned_by_claim'):
                with Session(bind=conn) as restore_db:
                    parents = prepare_job_write(restore_db, [data.suggested_job_id], {'printer_id': None})
                    job = restore_db.query(Job).filter(Job.id == data.suggested_job_id).with_for_update().one_or_none()
                    if (job is not None and getattr(job.status, 'value', job.status) == 'pending'
                            and admission_facts(conn, job.id, printer_id)['job'] == current['admission_facts']['job']):
                        job.printer_id = None
                        restore_db.flush()
                        reconcile_job_write(restore_db, parents)
            conn.execute(text('DELETE FROM system_config WHERE key=:key'), {'key':guard})
        if success:
            from core.audit import log_audit
            log_audit(db, 'job.dispatched', 'job', data.suggested_job_id, {'printer_id':printer_id})
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return result


def confirm_on_admission(conn, *, token, job):
    context = _context.get()
    if context is None:
        return
    key, record = context['key'], context['record']
    current = load(conn, key)
    if current is None or current.get('claim_token') or current['job_id'] != job['id'] or current['printer_id'] != job['printer_id']:
        raise RuntimeError('Start-next confirmation cannot be bound to this claim')
    if admission_facts(conn, job['id'], job['printer_id']) != current['admission_facts']:
        raise RuntimeError('Start-next suggestion changed before admission')
    at = datetime.now(timezone.utc)
    # Deliberately synchronous and on admission's connection. Any insert failure
    # rolls back both adapter_entered and its receipt; transport remains refused.
    conn.execute(text('INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,actor_user_id,actor_token_id,occurred_at) VALUES(:id,:job,:printer,:org,\'bed_clear_confirmed\',:detail,:user,:token,:at)'),
        {'id': uuid.uuid4().hex, 'job': job['id'], 'printer': job['printer_id'], 'org': job.get('charged_to_org_id'),
         'detail': json.dumps({'attestation': record['attestation']}),
         'user': record['actor']['user_id'], 'token': record['actor']['token_id'], 'at': at})
    current['claim_token'] = token
    current['result'] = {**current['result'], 'success': False, 'accepted': True, 'state': 'admitted',
                         'attempt_id': token, 'status_url': status_url(job["id"], token)}
    save(conn, key, current)
