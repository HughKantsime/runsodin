"""Explicit token delegation for local Square mapping and baseline records only."""
from typing import Annotated
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from pydantic import StringConstraints
from slowapi.util import get_remote_address
from sqlalchemy import select, text

from core.api_token_policy import SQUARE_SETUP_SCOPE, parse_token_scopes, require_square_setup_available
from core.audit import validate_setup_note
from core.db import get_db
from core.rbac import mark_token_scope_checker, require_action, require_role
from modules.orders import square_store as store

# The only routes admitted by this delegation (including their /api/v1 aliases).
SQUARE_SETUP_ROUTES = frozenset({
    ('PUT', '/products/{product_id}/square'),
    ('PUT', '/products/{product_id}/square/baseline'),
})
ApprovalNote = Annotated[str, StringConstraints(strict=True)]


def assert_square_setup(user):
    if not user or user.get('role') != 'admin':
        raise HTTPException(403, 'Administrator access is required')
    if user.get('_auth_kind') == 'user_api_token':
        if SQUARE_SETUP_SCOPE not in (user.get('_token_scopes') or []):
            raise HTTPException(403, 'This token needs Square setup permission')
    else:
        store.assert_interactive(user)
    return user


def require_square_setup():
    def checker(request: Request,
                user=Depends(require_role('admin', scope=SQUARE_SETUP_SCOPE)),
                action=Depends(require_action('products.square_manage', scope=SQUARE_SETUP_SCOPE)),
                db=Depends(get_db)):
        assert_square_setup(user)
        if user.get('_auth_kind') == 'user_api_token':
            path = request.scope['route'].path
            for prefix in ('/api/v1', '/api'):
                if path.startswith(prefix + '/'):
                    path = path[len(prefix):]
                    break
            if (request.method, path) not in SQUARE_SETUP_ROUTES:
                raise HTTPException(403, 'Square setup permission does not allow this action')
            require_square_setup_available(db)
        request.state.square_setup_actor = user
        return user
    return mark_token_scope_checker(checker, SQUARE_SETUP_SCOPE)


def setup_rate_key(request: Request):
    user = request.state.square_setup_actor
    if user.get('_auth_kind') == 'user_api_token':
        return 'square-setup-token:' + str(user['_token_id'])
    return get_remote_address(request)


def setup_rate_cost(request: Request):
    return int(request.state.square_setup_actor.get('_auth_kind') == 'user_api_token')


def setup_audit(user, note):
    if user.get('_auth_kind') != 'user_api_token':
        # Preserve session input validation while delegated records keep exact whitespace.
        if note is not None and not 1 <= len(note.strip()) <= 500:
            raise HTTPException(422, 'Invalid Square request. Check the required fields.')
        return {}
    validate_setup_note(note)
    return {'auth_kind': 'user_api_token', 'token_id': user['_token_id'],
            'token_name': user['_token_name'], 'token_owner_user_id': user['id'],
            'approval_note': note}


def fresh_setup_actor(db, user):
    """Caller holds the owner/product or connector lock after the Square read."""
    if user.get('_auth_kind') != 'user_api_token':
        return user
    account_query = select(text('id,role,group_id,is_active')).select_from(text('users')).where(text('id=:id'))
    token_query = select(text('user_id,name,scopes,expires_at')).select_from(text('api_tokens')).where(text('id=:id'))
    account = db.execute(account_query.with_for_update(), {'id': user['id']}).first()
    token = db.execute(token_query.with_for_update(), {'id': user['_token_id']}).first()
    if (not account or not account.is_active or account.role != 'admin'
            or account.group_id != user.get('group_id') or not token or token.user_id != account.id):
        raise HTTPException(403, 'Square setup permission is no longer available')
    if token.expires_at:
        try:
            expiry = datetime.fromisoformat(token.expires_at) if isinstance(token.expires_at, str) else token.expires_at
            expiry = expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry.astimezone(timezone.utc)
            expired = expiry <= datetime.now(timezone.utc)
        except (ValueError, TypeError, AttributeError, OverflowError):
            expired = True
        if expired:
            raise HTTPException(403, 'Square setup permission is no longer available')
    fresh = {**user, '_token_scopes': parse_token_scopes(token.scopes) or [], '_token_name': token.name}
    assert_square_setup(fresh)
    require_square_setup_available(db)
    from core.rbac import _is_platform_superadmin
    from core.rbac_policy import action_roles
    if not _is_platform_superadmin(fresh) and fresh['role'] not in action_roles(db, 'products.square_manage'):
        raise HTTPException(403, 'Insufficient permissions')
    return fresh


def same_mapping(doc, product_id, location, variation):
    mapping = doc['mappings'].get(str(product_id))
    if not mapping:
        return False
    if (mapping['location_id'], mapping['variation_id']) != (location, variation):
        raise HTTPException(409, 'This product is already mapped in Square. A signed-in administrator must change the mapping.')
    return True
