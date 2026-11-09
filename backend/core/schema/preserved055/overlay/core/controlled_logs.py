"""Bounded log auditing and short-lived, actor-bound stream acknowledgements."""
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import HTTPException
from sqlalchemy import text
from core.controlled_parts import any_marks
from core.audit import audit_download


def request_ack(request):
    from core.controlled_csrf import require_controlled_ack
    acknowledged = request.headers.get('x-odin-controlled-ack') == 'true'
    if acknowledged:
        require_controlled_ack(request)
    return acknowledged


def check_log_access(db, actor, acknowledged, *, audit=True, audit_success=True):
    if not any_marks(db):
        return
    code = None
    if acknowledged is not True:
        code = 'controlled_part_acknowledgement_required'
    elif not actor or actor.get('role') != 'admin' or actor.get('group_id') is not None:
        code = 'controlled_part_superadmin_required'
    if code:
        if audit:
            # Serialize the per-user hourly refusal check across workers.
            from core.models import AuditLog
            uid = actor.get('id') if actor else None
            db.execute(text('UPDATE users SET id=id WHERE id=:uid'), {'uid': uid})
            recent = db.query(AuditLog.id).filter(
                AuditLog.action == 'controlled_part.refused', AuditLog.entity_type == 'log_viewer',
                AuditLog.entity_id == uid,
                AuditLog.timestamp >= datetime.now(timezone.utc) - timedelta(hours=1)).first()
            if not recent:
                audit_download(db, 'controlled_part.refused', 'log_viewer', uid, {'reason': code})
            else:
                db.rollback()
        raise HTTPException(403, {'code': code, 'message': 'Controlled part access refused'})
    if audit and audit_success:
        audit_download(db, 'controlled_part.privileged_export', 'system', None,
                       {'kind': 'application_logs', 'acknowledged': True})


def issue_stream_token(actor, acknowledged):
    from core.auth import SECRET_KEY
    return jwt.encode({'sub': str(actor['id']), 'purpose': 'controlled_log_stream',
                       'ack': acknowledged, 'exp': datetime.now(timezone.utc) + timedelta(minutes=5)},
                      SECRET_KEY, algorithm='HS256')


def stream_ack(token, actor):
    from core.auth import SECRET_KEY
    try:
        claims = jwt.decode(token, SECRET_KEY, algorithms=['HS256'], options={'require': ['exp', 'sub']})
        if claims.get('purpose') != 'controlled_log_stream' or claims['sub'] != str(actor['id']):
            raise ValueError('Wrong stream audience')
        return claims.get('ack') is True
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(403, {'code': 'controlled_part_stream_token_required'}) from None


def stream_log_access(engine, actor, acknowledged):
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        try:
            current = db.execute(text('SELECT role,group_id,is_active FROM users WHERE id=:id'), {'id': actor['id']}).mappings().first()
            if not current or not current['is_active'] or current['role'] != 'admin' or current['group_id'] is not None:
                raise HTTPException(403, {'code': 'controlled_part_stream_authority_changed'})
            check_log_access(db, dict(current), acknowledged, audit=False)
        except HTTPException as exc:
            audit_download(db, 'controlled_part.log_stream_ended', 'log_viewer', actor['id'], exc.detail)
            raise
