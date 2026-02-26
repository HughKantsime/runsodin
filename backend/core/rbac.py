"""
O.D.I.N. — RBAC and org scoping dependencies.

Provides FastAPI dependencies for role-based access control and
organisation-level resource scoping.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import require_role) continues to work via re-exports in deps.py.
"""

from typing import Optional

from fastapi import Depends, HTTPException

from auth import has_permission


def _get_org_filter(current_user: dict, request_org_id: int = None) -> Optional[int]:
    """Determine the effective org_id for filtering resources."""
    group_id = current_user.get("group_id") if current_user else None
    role = current_user.get("role", "viewer") if current_user else "viewer"

    if role == "admin" and not group_id:
        return request_org_id  # Superadmin — can optionally filter by org
    if role == "admin" and request_org_id is not None:
        return request_org_id  # Admin overriding their own group scope
    return group_id  # Regular user or org-scoped admin


def get_org_scope(current_user: dict) -> Optional[int]:
    """Return the org_id that should implicitly scope all resource access.

    - Superadmin (role=admin, no group_id): returns None (see everything)
    - Everyone else: returns their group_id (may be None if unassigned)

    For detail endpoints, use ``check_org_access()`` to verify a specific
    resource belongs to the caller's org.
    """
    if not current_user:
        return None
    role = current_user.get("role", "viewer")
    group_id = current_user.get("group_id")
    if role == "admin" and not group_id:
        return None  # superadmin bypass
    return group_id


def check_org_access(current_user: dict, resource_org_id: Optional[int]) -> bool:
    """Check whether the current user may access a resource with the given org_id.

    Rules:
    - Superadmin (admin + no group_id): always True
    - Resource has no org_id (NULL): visible to everyone
    - User's group_id matches resource org_id: True
    - Otherwise: False  (caller should raise 404 to avoid leaking existence)
    """
    if not current_user:
        return False
    role = current_user.get("role", "viewer")
    group_id = current_user.get("group_id")
    # Superadmin sees everything
    if role == "admin" and not group_id:
        return True
    # Unscoped resources are visible to all authenticated users
    if resource_org_id is None:
        return True
    # User must belong to the resource's org
    return group_id is not None and group_id == resource_org_id


def require_role(required_role: str, scope: str = None):
    """FastAPI dependency that checks the user has at least the given role.

    Optionally enforces an API token scope (for per-user scoped tokens).
    scope='read' for GET endpoints, scope='write' for POST/PATCH/PUT/DELETE.
    Scope is only enforced when the request uses a per-user API token (odin_xxx).
    JWT and global API key bypass scope checks.
    """
    # Import here to avoid a circular dependency between core.rbac and core.dependencies
    from core.dependencies import get_current_user

    async def role_checker(current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not has_permission(current_user["role"], required_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        # Scope enforcement for scoped per-user API tokens
        if scope is not None:
            token_scopes = current_user.get("_token_scopes", [])
            if token_scopes:
                granted = (
                    scope in token_scopes
                    or any(s.startswith(f"{scope}:") for s in token_scopes)
                    or "admin" in token_scopes
                )
                if not granted:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Insufficient token scope — '{scope}' scope required",
                    )
        return current_user
    return role_checker


def require_scope(scope: str):
    """FastAPI dependency that enforces API token scope for per-user token auth.

    Only enforced when the request uses a per-user API token (odin_xxx format).
    JWT session auth and the global API key bypass scope checks (they have full access).
    Scope values: 'read', 'write', 'admin'.

    Usage:
        @router.delete("/things/{id}")
        async def delete_thing(current_user: dict = Depends(require_scope("write"))):
            ...
    """
    # Import here to avoid a circular dependency between core.rbac and core.dependencies
    from core.dependencies import get_current_user

    async def _check(current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        token_scopes = current_user.get("_token_scopes", [])
        # Only enforce scopes when the user authenticated via a scoped per-user token.
        # Empty list means JWT or global API key — no scope restriction.
        if token_scopes:
            # Match: exact scope ("write"), umbrella ("write" covers "write:printers"), or "admin"
            granted = (
                scope in token_scopes
                or any(s.startswith(f"{scope}:") for s in token_scopes)
                or "admin" in token_scopes
            )
            if not granted:
                raise HTTPException(
                    status_code=403,
                    detail=f"Insufficient token scope — '{scope}' scope required",
                )
        return current_user
    return _check
