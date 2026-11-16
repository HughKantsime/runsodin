"""Explicit token delegation for local Square mapping and baseline records only."""
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import StringConstraints
from slowapi.util import get_remote_address

from core.api_token_policy import SQUARE_SETUP_SCOPE, require_square_setup_available
from core.db import get_db
from core.rbac import mark_token_scope_checker, require_action, require_role
from modules.orders import square_store as store

# The only routes admitted by this delegation (including their /api/v1 aliases).
SQUARE_SETUP_ROUTES = frozenset({
    ('PUT', '/products/{product_id}/square'),
    ('PUT', '/products/{product_id}/square/baseline'),
})
ApprovalNote = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]


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
        return {}
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
        raise HTTPException(422, 'Record who approved this setup, where and when (1 to 500 characters)')
    return {'auth_kind': 'user_api_token', 'token_id': user['_token_id'],
            'token_name': user['_token_name'], 'token_owner_user_id': user['id'],
            'approval_note': note}
