"""Current request-bound financial reader; no commit or provider fallback."""
from dataclasses import dataclass
from datetime import datetime, timezone
import jwt
from fastapi import HTTPException
from sqlalchemy import text
from core.audit_actor import _bound_postgres_actor_locks
from core.api_token_policy import parse_token_scopes, token_scope_granted
from core.auth import has_permission
from core.global_order_creation import has_verified_origin_request


@dataclass(frozen=True)
class FinancialReader:
    id: int
    username: str
    role: str
    group_id: int | None
    auth_kind: str


def _refuse():
    raise HTTPException(403, 'Sign in again to verify financial read authority')


def verify_financial_reader(db, request, actor):
    if type(actor) is not dict or not has_verified_origin_request(db, request, actor):
        _refuse()
    identifier = actor.get('id')
    if type(identifier) is not int or identifier <= 0:
        _refuse()
    postgres = db.get_bind().dialect.name == 'postgresql'
    kind = actor['_auth_kind']
    if postgres:
        _bound_postgres_actor_locks(db.connection())
        if kind != 'user_api_token':
            db.execute(text('LOCK TABLE public.token_blacklist IN SHARE MODE NOWAIT'))
        suffix = ' FOR UPDATE NOWAIT'
    else:
        db.execute(text('UPDATE users SET id=id WHERE id=:id'), {'id': identifier})
        suffix = ''
    user = db.execute(text('SELECT id,username,role,group_id,is_active FROM users WHERE id=:id' + suffix), {'id': identifier}).first()
    if (not user or user.username != actor.get('username') or not has_permission(user.role, 'viewer')
            or user.role != actor.get('role') or user.group_id != actor.get('group_id') or not user.is_active):
        _refuse()
    if kind == 'user_api_token':
        from core.auth import verify_password
        raw = request.headers.get('X-API-Key')
        token_id = actor.get('_token_id')
        if not isinstance(raw, str) or not raw.startswith('odin_') or type(token_id) is not int:
            _refuse()
        token = db.execute(text('SELECT id,user_id,token_prefix,token_hash,scopes,expires_at FROM api_tokens WHERE id=:id' + suffix), {'id': token_id}).first()
        if not token or token.user_id != identifier or token.token_prefix != raw[:10] or not verify_password(raw, token.token_hash):
            _refuse()
        scopes = parse_token_scopes(token.scopes)
        if scopes is None or not token_scope_granted(scopes, 'read') or not token_scope_granted(scopes, 'read:spools'):
            _refuse()
        if token.expires_at is not None:
            try:
                expiry = token.expires_at if isinstance(token.expires_at, datetime) else datetime.fromisoformat(token.expires_at)
                expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry.astimezone(timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    _refuse()
            except (ValueError, TypeError):
                _refuse()
    else:
        from core.dependencies import validate_access_token
        from core import auth
        if kind == 'session_jwt':
            raw = request.cookies.get('session')
        else:
            header = request.headers.get('Authorization', '')
            scheme, _, raw = header.partition(' ')
            if scheme.lower() != 'bearer':
                _refuse()
        current = validate_access_token(raw, db)
        if not current or current['id'] != identifier or current['username'] != user.username:
            _refuse()
        try:
            payload = jwt.decode(raw, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        except jwt.PyJWTError:
            _refuse()
        jti = payload.get('jti')
        if not isinstance(jti, str) or not 0 < len(jti) <= 128:
            _refuse()
        if kind == 'session_jwt':
            # Deliberately stricter for financial attestation than legacy cookie
            # admission: a genuine tracked login is required for financial reads.
            session = db.execute(text('SELECT user_id FROM active_sessions WHERE token_jti=:jti' + suffix), {'jti': jti}).first()
            if not session or session.user_id != identifier:
                _refuse()

    return FinancialReader(user.id, user.username, user.role, user.group_id, kind)
