"""
O.D.I.N. — RBAC and org scoping dependencies.

Provides FastAPI dependencies for role-based access control and
organisation-level resource scoping.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import require_role) continues to work via re-exports in deps.py.
"""

from typing import Optional

from fastapi import Depends, HTTPException

from core.auth import has_permission


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


def require_superadmin():
    """FastAPI dependency that requires superadmin (admin role + no group_id).

    Use for system-wide operations: config, backup/restore, license, OIDC, etc.
    Org-scoped admins (admin role + group_id) are rejected with 403.
    """
    from core.dependencies import get_current_user

    async def superadmin_checker(current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if current_user.get("group_id"):
            raise HTTPException(status_code=403, detail="Superadmin access required")
        return current_user
    return superadmin_checker


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


def require_any_scope(*allowed: str):
    """FastAPI dependency: accept if the token has ANY of the listed scopes.

    v1.8.9 agent-surface routes use this to express "admin OR agent:write"
    concisely:

        @router.post("/queue/add")
        async def add(current_user = Depends(require_any_scope("admin", "agent:write"))):
            ...

    Semantics (matching existing `require_scope` for compat):
    - JWT / cookie / global-API-key sessions bypass the check (full access).
    - Per-user scoped tokens (odin_xxx): grant if any of `allowed` is in
      the token's scopes list OR the token has `admin`.
    - Umbrella check (`scope:*` matches `scope`) is kept for read/write.
      agent:* scopes are treated as exact matches only — they don't grant
      `read` or `write` by themselves, and `read`/`write` don't grant
      `agent:*`. This is intentional: agent tokens are a narrower grant.

    Raises OdinError with code `scope_denied` on failure so the MCP
    tool layer can surface a clean "re-auth required" path.
    """
    from core.dependencies import get_current_user
    from core.errors import OdinError, ErrorCode

    async def _check(current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise OdinError(
                ErrorCode.not_authenticated,
                "Not authenticated",
                status=401,
            )
        token_scopes = current_user.get("_token_scopes", []) or []
        if not token_scopes:
            # Non-token auth (JWT, cookie, global key) — full access.
            return current_user
        if "admin" in token_scopes:
            return current_user
        for scope in allowed:
            if scope in token_scopes:
                return current_user
            # Umbrella match only for read/write, not agent:*
            if scope in ("read", "write") and any(
                s.startswith(f"{scope}:") for s in token_scopes
            ):
                return current_user
        raise OdinError(
            ErrorCode.scope_denied,
            f"Token scope insufficient; one of {list(allowed)} required",
            status=403,
            extra={"allowed_scopes": list(allowed), "token_scopes": list(token_scopes)},
        )

    return _check


# Well-known scope strings for the v1.8.9 agent surface. Tokens can be
# minted with any subset of these via the admin token-creation UI.
AGENT_READ_SCOPE = "agent:read"
AGENT_WRITE_SCOPE = "agent:write"

# Convenience dependencies matching the two common agent-surface cases.
def agent_read_or_above():
    """Shortcut: admin OR agent:read OR agent:write."""
    return require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE)

def agent_write_or_above():
    """Shortcut: admin OR agent:write."""
    return require_any_scope("admin", AGENT_WRITE_SCOPE)
