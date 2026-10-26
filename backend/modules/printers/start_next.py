"""Owner-confirmed dispatch, with durable idempotency and admission attestation."""
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import uuid
from fastapi import HTTPException
from sqlalchemy import text, bindparam, JSON
from sqlalchemy.orm import Session
from core.system_config_values import config_text
from core.rbac import check_org_access
from modules.jobs.completion_summary import next_suggestion

_context = ContextVar('start_next_confirmation', default=None)


def request_key(request_id):
    return 'start_next:' + str(request_id)


def load(conn, key):
    raw = conn.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': key}).scalar_one_or_none()
    return json.loads(config_text(raw)) if raw is not None else None


def insert(conn, key, value):
    param = bindparam('value', type_=JSON) if conn.dialect.name == 'postgresql' else bindparam('value')
    return conn.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) ON CONFLICT(key) DO NOTHING').bindparams(param),
                        {'key': key, 'value': json.dumps(value, sort_keys=True)}).rowcount == 1


def save(conn, key, value):
    param = bindparam('value', type_=JSON) if conn.dialect.name == 'postgresql' else bindparam('value')
    conn.execute(text('UPDATE system_config SET value=:value WHERE key=:key').bindparams(param),
                 {'key': key, 'value': json.dumps(value, sort_keys=True)})


def principal(user):
    return {'user_id': user.get('id'), 'token_id': user.get('_token_id'), 'org_id': user.get('group_id')}


def replay(record, user, printer_id):
    if record['actor'] != principal(user) or record['printer_id'] != printer_id:
        raise HTTPException(409, 'Request id belongs to a different operation')
    return record['result']


def start(db, printer, user, data):
    # First operation is the durable idempotency lookup. Replays never consult
    # queue state, fingerprints or mutable printer facts.
    printer_id = printer.id
    key = request_key(data.request_id)
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
        with Session(bind=conn) as fresh:
            from modules.printers.models import Printer
            current = fresh.get(Printer, printer_id)
            suggestion = next_suggestion(fresh, current, visible=visible)
        if suggestion['job_id'] != data.suggested_job_id or suggestion['fingerprint'] != data.suggestion_fingerprint:
            raise HTTPException(409, {'message': 'Suggestion changed.', 'suggestion': suggestion})
        # The guard binds the exact candidate/revision and finished attempt to a
        # single request. A crash retains the intent and cannot authorize a resend.
        guard = 'start_next_claim:' + hashlib.sha256(f'{printer_id}:{data.suggestion_fingerprint}'.encode()).hexdigest()
        if not insert(conn, guard, {'request_id': str(data.request_id)}):
            raise HTTPException(409, {'message': 'This suggestion already has a start request.', 'suggestion': suggestion})
        if not insert(conn, key, record):
            return replay(load(conn, key), user, printer_id)
        changed = conn.execute(text("UPDATE jobs SET printer_id=:printer WHERE id=:job AND status='pending' AND hold IS FALSE AND (printer_id IS NULL OR printer_id=:printer)"),
                               {'job': data.suggested_job_id, 'printer': printer_id}).rowcount
        if changed != 1:
            raise HTTPException(409, 'Candidate changed before it could be claimed')
    marker = _context.set({'key': key, 'record': record, 'user': user})
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
    with engine.begin() as conn:
        current = load(conn, key)
        current['result'] = result
        save(conn, key, current)
        if not success and not current.get('claim_token'):
            conn.execute(text('DELETE FROM system_config WHERE key=:key'), {'key':guard})
    return result


def confirm_on_admission(conn, *, token, job):
    context = _context.get()
    if context is None:
        return
    key, record = context['key'], context['record']
    current = load(conn, key)
    if current is None or current.get('claim_token') or current['job_id'] != job['id'] or current['printer_id'] != job['printer_id']:
        raise RuntimeError('Start-next confirmation cannot be bound to this claim')
    from modules.printers.models import Printer
    with Session(bind=conn) as db:
        suggestion = next_suggestion(db, db.get(Printer, job['printer_id']), visible=lambda j: check_org_access(context['user'], j.charged_to_org_id))
    if suggestion['job_id'] != job['id'] or suggestion['fingerprint'] != record['fingerprint']:
        raise RuntimeError('Start-next suggestion changed before admission')
    at = datetime.now(timezone.utc)
    # Deliberately synchronous and on admission's connection. Any insert failure
    # rolls back both adapter_entered and its receipt; transport remains refused.
    conn.execute(text('INSERT INTO job_timeline_events(id,job_id,printer_id,org_id,kind,detail,actor_user_id,actor_token_id,occurred_at) VALUES(:id,:job,:printer,:org,\'bed_clear_confirmed\',:detail,:user,:token,:at)'),
        {'id': uuid.uuid4().hex, 'job': job['id'], 'printer': job['printer_id'], 'org': job.get('charged_to_org_id'),
         'detail': json.dumps({'attestation': record['attestation'], 'claim_token': token, 'request_id': key.split(':',1)[1]}),
         'user': record['actor']['user_id'], 'token': record['actor']['token_id'], 'at': at})
    current['claim_token'] = token
    current['result'] = {**current['result'], 'success': True, 'state': 'admitted'}
    save(conn, key, current)
