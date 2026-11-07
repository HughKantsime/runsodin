"""Private selected-carrier authority for explicit plug requests; no fallback."""
from dataclasses import dataclass, field, replace
import hmac
import os
import re
from sqlalchemy import text
from sqlalchemy.orm import Session
from modules.printers.power_fence import _digest

_SEAL = object()


@dataclass(frozen=True)
class ManualPowerAuthority:
    engine: object = field(repr=False)
    printer_id: int
    purpose: str
    request_nonce: str
    auth_kind: str
    actor_user_id: int
    token_id: int | None
    credential: str = field(repr=False)
    binding: str = field(repr=False)
    seal: object = field(repr=False)

    def _current(self, connection=None):
        from core.dependencies import validate_access_token, _principal
        from core.rbac import check_org_access, _check_token_scope, _is_platform_superadmin
        from core.rbac_policy import action_roles
        with Session(connection if connection is not None else self.engine) as db:
            # Short admission credential predicates only, never held over I/O.
            if db.bind.dialect.name == 'postgresql':
                db.execute(text("SET LOCAL lock_timeout='2000ms'"))
                tables = ('api_tokens', 'users') if self.auth_kind == 'scoped_api_key' else (
                    ('token_blacklist', 'users') if self.auth_kind in ('session_jwt', 'bearer_jwt') else ('users',))
                db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe: internal constant SQL fragments only; all values bound
                    'LOCK TABLE ' + ','.join(tables) + ' IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
            if self.auth_kind in ('session_jwt', 'bearer_jwt'):
                principal = validate_access_token(self.credential, db)
            elif self.auth_kind == 'scoped_api_key':
                principal = _scoped_principal(db, self.credential)
            elif self.auth_kind == 'legacy_global_api_key':
                configured = os.environ.get('API_KEY', '')
                if not configured or not hmac.compare_digest(configured, self.credential):
                    return None
                row = db.execute(text("SELECT * FROM users WHERE role='admin' AND is_active IS TRUE ORDER BY id LIMIT 1")).first()
                principal = _principal(row, self.auth_kind)
            else:
                return None
            if (not principal or principal['id'] != self.actor_user_id
                    or self.auth_kind == 'scoped_api_key' and principal.get('_token_id') != self.token_id):
                return None
            roles = action_roles(db, 'printers.plug')
            if not _is_platform_superadmin(principal) and principal.get('role') not in roles:
                return None
            _check_token_scope(principal, 'write:printers')
            printer = db.execute(text('SELECT org_id FROM printers WHERE id=:id'), {'id': self.printer_id}).first()
            if printer is None or not check_org_access(principal, printer[0]):
                return None
            # Bind account lifetime/permission snapshot and exact selected carrier.
            return _digest([self.credential, self.auth_kind, dict(principal), sorted(roles), printer[0]])

    def valid(self, connection=None):
        return self.seal is _SEAL and self._current(connection) == self.binding

    def facts(self, facts):
        if not self.valid() or facts.printer_id != self.printer_id or facts.purpose != self.purpose:
            raise ValueError('Manual power authority changed')
        return replace(facts, request_nonce=self.request_nonce, actor_user_id=self.actor_user_id,
                       auth_kind=self.auth_kind, authority_key=self.binding)


def capture(engine, request, principal, printer_id, purpose):
    nonce = request.headers.get('X-Power-Request-Nonce', '')
    if not nonce:
        # Older API clients send no nonce: the command is still fenced, but a
        # retried request is a new command rather than an idempotent replay.
        import uuid
        nonce = uuid.uuid4().hex
    elif re.fullmatch('[0-9a-f]{32}', nonce) is None:
        raise ValueError('X-Power-Request-Nonce must be 32 lowercase hex characters')
    kind = principal.get('_auth_kind')
    if kind == 'user_api_token':
        kind = 'scoped_api_key'
    if kind == 'session_jwt':
        credential = request.cookies.get('session', '')
    elif kind == 'bearer_jwt':
        parts = request.headers.get('Authorization', '').split()
        credential = parts[1] if len(parts) == 2 and parts[0].lower() == 'bearer' else ''
    elif kind in ('scoped_api_key', 'legacy_global_api_key'):
        credential = request.headers.get('X-API-Key', '')
    else:
        raise ValueError('Selected power credential is required')
    if not credential:
        raise ValueError('Selected power credential is required')
    authority = ManualPowerAuthority(engine, printer_id, purpose, nonce, kind,
                                     principal['id'], principal.get('_token_id'), credential, '', _SEAL)
    binding = authority._current()
    if binding is None:
        raise ValueError('Selected power authority could not be verified')
    return replace(authority, binding=binding)


def _scoped_principal(db, credential):
    # Same selected-token validation as the request resolver, without its
    # best-effort last_used_at commit (which would release admission predicates).
    from datetime import datetime, timezone
    from core.auth import verify_password
    from core.dependencies import _principal
    from core.api_token_policy import parse_token_scopes, is_legacy_camera_combination
    if not credential.startswith('odin_'):
        return None
    candidates = db.execute(text('SELECT * FROM api_tokens WHERE token_prefix=:prefix'),
                            {'prefix': credential[:10]}).all()
    matched = None
    for candidate in candidates:
        good = verify_password(credential, candidate.token_hash)
        if candidate.expires_at:
            try:
                from modules.printers.power_fence import _utc
                good = good and _utc(candidate.expires_at) > datetime.now(timezone.utc)
            except Exception:
                good = False
        if good and matched is None:
            matched = candidate
    if matched is None:
        return None
    user = db.execute(text('SELECT * FROM users WHERE id=:id'), {'id': matched.user_id}).first()
    scopes = parse_token_scopes(matched.scopes)
    if not user or scopes is None or is_legacy_camera_combination(scopes):
        return None
    if 'admin' in scopes and user.role != 'admin' or 'agent:write' in scopes and user.role not in ('admin', 'operator'):
        return None
    principal = _principal(user, 'scoped_api_key', token_id=int(matched.id), token_scopes=scopes)
    if principal:
        principal['_token_expires_at'] = matched.expires_at
    return principal
