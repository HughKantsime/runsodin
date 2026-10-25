"""Explicit delegation for inspected production stock, never connector setup."""
from fastapi import Depends, HTTPException
from core.rbac import mark_token_scope_checker

PRODUCTION_SCOPE = 'write:production'


def assert_production_operator(user):
    if not user:
        raise HTTPException(401, 'Not authenticated')
    if user.get('role') not in ('admin', 'operator'):
        raise HTTPException(403, 'Production operator access is required')
    kind = user.get('_auth_kind')
    scopes = user.get('_token_scopes') or []
    if kind in ('jwt', 'session_jwt', 'bearer_jwt') and not scopes:
        return user
    if kind == 'user_api_token' and PRODUCTION_SCOPE in scopes:
        return user
    raise HTTPException(403, 'Sign in with a password session or use an explicit write:production operator token')


def require_production_operator():
    from core.dependencies import get_current_user

    async def checker(user=Depends(get_current_user)):
        return assert_production_operator(user)

    return mark_token_scope_checker(checker, PRODUCTION_SCOPE)
