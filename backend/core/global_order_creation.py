"""Private prospective company-order creator; caller owns the transaction."""
from datetime import datetime, timezone
from uuid import uuid4
import jwt
from fastapi import HTTPException
from sqlalchemy import Column, MetaData, Text, inspect, text
from core.audit_actor import _BINDING, _SCOPE_KEY, ActorSnapshot, RequestAuditScope, _bound_postgres_actor_locks
from core.api_token_policy import parse_token_scopes, token_scope_granted

KINDS = frozenset(('session_jwt', 'bearer_jwt', 'user_api_token'))
FIELDS = ('order_number', 'platform', 'customer_name', 'customer_email', 'order_date',
          'notes', 'revenue', 'platform_fees', 'payment_fees', 'shipping_charged',
          'shipping_cost', 'labor_minutes', 'org_id')


def has_verified_origin_request(db, request, actor):
    if request is None or actor.get('_auth_kind') not in KINDS:
        return False
    binding = db.info.get(_BINDING)
    if not binding or type(binding[0]) is not RequestAuditScope or type(binding[1]) is not ActorSnapshot:
        return False
    scope, snapshot = binding
    return (scope.live and db in scope.sessions and request.scope.get(_SCOPE_KEY) is scope
            and (snapshot.user_id, snapshot.username, snapshot.auth_kind)
            == (actor.get('id'), actor.get('username'), actor.get('_auth_kind')))


def _refuse():
    raise HTTPException(403, 'Sign in again to verify company-order authority')


def _locked_actor(db, request, actor):
    if not has_verified_origin_request(db, request, actor):
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
    user = db.execute(text('SELECT id,username,role,group_id,is_active FROM users WHERE id=:id' + suffix), {'id': identifier}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant dialect lock suffix is appended; values are bound
    if (not user or user.username != actor.get('username') or user.role != 'admin'
            or user.group_id is not None or not user.is_active):
        _refuse()
    if kind == 'user_api_token':
        from core.auth import verify_password
        raw = request.headers.get('X-API-Key')
        token_id = actor.get('_token_id')
        if not isinstance(raw, str) or not raw.startswith('odin_') or type(token_id) is not int:
            _refuse()
        token = db.execute(text('SELECT id,user_id,token_prefix,token_hash,scopes,expires_at FROM api_tokens WHERE id=:id' + suffix), {'id': token_id}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant dialect lock suffix is appended; values are bound
        if not token or token.user_id != identifier or token.token_prefix != raw[:10] or not verify_password(raw, token.token_hash):
            _refuse()
        scopes = parse_token_scopes(token.scopes)
        if scopes is None or not token_scope_granted(scopes, 'write'):
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
            # admission: a genuine tracked login is required for new origins.
            session = db.execute(text('SELECT user_id FROM active_sessions WHERE token_jti=:jti' + suffix), {'jti': jti}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant dialect lock suffix is appended; values are bound
            if not session or session.user_id != identifier:
                _refuse()


def create_attested_global_order(db, resource, request, actor):
    from modules.orders.models import Order
    state = inspect(resource)
    if (type(resource) is not Order or not state.transient or resource.id is not None
            or resource.org_id is not None or getattr(resource, 'economic_uid', None) is not None
            or getattr(resource, 'economic_global_origin_nonce', None) is not None):
        raise ValueError('Fresh global order required')
    _locked_actor(db, request, actor)
    nonce, uid = uuid4().hex, str(uuid4())
    table = Order.__table__.to_metadata(MetaData())
    table.append_column(Column('economic_global_origin_nonce', Text))
    values = {name: getattr(resource, name) for name in FIELDS}
    identifier = db.execute(table.insert().values(**values, economic_global_origin_nonce=nonce).returning(table.c.id)).scalar_one()
    db.execute(text("INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(:uid,'order',:id,'global',0)"), {'uid': uid, 'id': identifier})
    changed = db.execute(text('UPDATE orders SET economic_uid=:uid WHERE id=:id AND economic_uid IS NULL AND org_id IS NULL AND economic_global_origin_nonce=:nonce'), {'uid': uid, 'id': identifier, 'nonce': nonce})
    if changed.rowcount != 1:
        raise RuntimeError('Fresh company-order identity changed')
    db.execute(text("INSERT INTO cost_global_order_origins(order_uid,origin_nonce,resource_kind,owner_scope,owner_key,proof_kind) VALUES(:uid,:nonce,'order','global',0,'verified_platform_admin_global_creation')"), {'uid': uid, 'nonce': nonce})
    return db.query(Order).filter(Order.id == identifier).one()
