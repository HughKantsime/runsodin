"""
O.D.I.N. — RBAC and org scoping dependencies.

Provides FastAPI dependencies for role-based access control and
organisation-level resource scoping.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import require_role) continues to work via re-exports in deps.py.
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request

from core.auth import has_permission
from core.api_token_policy import token_scope_granted


# ---------------------------------------------------------------------------
# Token-scope policy for per-user API tokens (odin_xxx)
# ---------------------------------------------------------------------------
#
# Every route reached with a scoped per-user token must be covered by a scope
# check. Routes that name a scope (require_role(..., scope=...),
# require_scope, require_any_scope, the Education principals) check that
# scope. Every other route requires the generic scope derived from the HTTP
# method: "read" for safe methods, "write" for anything that can change state.
# Resource-only scopes (read:printers, write:jobs, ...) therefore reach only
# routes that declare that resource; they never fall through to unrelated
# admin or user-management routes. JWT sessions and the global API key are
# not scoped credentials and are unaffected.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# require_role(..., scope=HANDLER_SCOPE): the route handler applies its own
# per-resource token-scope filters (for example the global search).
HANDLER_SCOPE = "handler"
_SCOPE_MARK = "__odin_token_scope__"


def method_token_scope(method: str) -> str:
    """Generic scope a token needs for a route that declares no scope."""
    return "read" if (method or "").upper() in SAFE_METHODS else "write"


def is_scoped_token(current_user: Optional[dict]) -> bool:
    return bool(current_user) and (
        bool(current_user.get("_token_scopes")) or current_user.get("_auth_kind") == "user_api_token"
    )


def mark_token_scope_checker(fn, scope):
    setattr(fn, _SCOPE_MARK, scope or "method")
    return fn


def _check_token_scope(current_user: dict, scope: str) -> None:
    if scope == HANDLER_SCOPE or not is_scoped_token(current_user):
        return
    if not token_scope_granted(current_user.get("_token_scopes", []) or [], scope):
        raise HTTPException(
            status_code=403,
            detail=f"Insufficient token scope — '{scope}' scope required",
        )


def handler_enforces_token_scope(*scopes: str):
    """Mark a route whose handler checks the named token scopes itself.

    Use only when the route cannot use a scope-checking dependency (for
    example media routes that also accept a query-string token).
    """
    async def _declared():
        return None
    return mark_token_scope_checker(_declared, ",".join(scopes) or HANDLER_SCOPE)


_route_scope_cache: dict = {}


def _route_scope_marks(route) -> frozenset:
    """Scope marks of every dependency of the route ("method" = generic)."""
    key = id(route)
    cached = _route_scope_cache.get(key)
    if cached is not None and cached[0] is route:
        return cached[1]
    marks = set()
    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        dep = stack.pop()
        mark = getattr(dep.call, _SCOPE_MARK, None)
        if mark:
            marks.add(mark)
        stack.extend(dep.dependencies or [])
    result = frozenset(marks)
    _route_scope_cache[key] = (route, result)
    return result


def route_declares_token_scope(route) -> bool:
    """True when a dependency of the route performs a token-scope check."""
    return bool(_route_scope_marks(route))


def route_declares_explicit_scope(route) -> bool:
    """True when a dependency of the route checks a named (non-generic) scope."""
    return any(mark != "method" for mark in _route_scope_marks(route))


def _generic_scope_check(request: Request, current_user: dict) -> None:
    """Method-derived scope check for require_role()/require_superadmin() without a scope.

    A stacked dependency that names its own scope (for example
    require_any_scope("read:printers", ...)) governs the route instead.
    """
    if not is_scoped_token(current_user):
        return
    if route_declares_explicit_scope(request.scope.get("route")):
        return
    _check_token_scope(current_user, method_token_scope(request.method))


def enforce_route_token_scope(request: Optional[Request], current_user: Optional[dict]) -> None:
    """Fail closed for scoped tokens on routes without a scope-checking dependency.

    Called by get_current_user, so routes that depend on it directly (for
    example the push and alert-preference routes) still need the method's
    generic scope.
    """
    if not is_scoped_token(current_user):
        return
    route = request.scope.get("route") if request is not None else None
    if route is not None and route_declares_token_scope(route):
        return
    method = request.method if request is not None else "POST"
    _check_token_scope(current_user, method_token_scope(method))


def _get_org_filter(current_user: dict, request_org_id: int = None) -> Optional[int]:
    """Determine the effective org_id for filtering resources."""
    group_id = current_user.get("group_id") if current_user else None
    role = current_user.get("role", "viewer") if current_user else "viewer"

    if role == "admin" and not group_id:
        return request_org_id  # Superadmin — can optionally filter by org
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



def should_filter_org(current_user: dict, effective_org: Optional[int]) -> bool:
    """Only a known superadmin may omit tenant predicates.

    None is also the scope of an unassigned user, who may see shared resources
    but must not gain access to private organizations through list queries.
    """
    if effective_org is not None:
        return True
    return not (current_user and current_user.get("role") == "admin"
                and not current_user.get("group_id"))

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

    Also enforces the API token scope for per-user scoped tokens (odin_xxx):
    the given scope, or — when none is given — "read" for GET/HEAD/OPTIONS and
    "write" for every other method. scope=HANDLER_SCOPE leaves the scope check
    to the route handler. JWT sessions and the global API key bypass scope
    checks.
    """
    # Import here to avoid a circular dependency between core.rbac and core.dependencies
    from core.dependencies import get_current_user

    async def role_checker(request: Request, current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not has_permission(current_user["role"], required_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if scope:
            _check_token_scope(current_user, scope)
        else:
            _generic_scope_check(request, current_user)
        return current_user
    return mark_token_scope_checker(role_checker, scope)


def require_superadmin(scope: str = None):
    """FastAPI dependency that requires superadmin (admin role + no group_id).

    Use for system-wide operations: config, backup/restore, license, OIDC, etc.
    Org-scoped admins (admin role + group_id) are rejected with 403.
    Scoped credentials need the given scope or, when none is given, the
    method's generic scope ("read" for safe methods, otherwise "write").
    """
    from core.dependencies import get_current_user

    async def superadmin_checker(request: Request, current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if current_user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if current_user.get("group_id"):
            raise HTTPException(status_code=403, detail="Superadmin access required")
        if scope:
            _check_token_scope(current_user, scope)
        else:
            _generic_scope_check(request, current_user)
        return current_user
    return mark_token_scope_checker(superadmin_checker, scope)


def require_scope(scope: str):
    """FastAPI dependency that enforces API token scope for per-user token auth.

    Only enforced when the request uses a per-user API token (odin_xxx format).
    JWT session auth and the global API key bypass scope checks (they have full access).
    Scope values include read/write, resource permissions, and admin.

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
        # Actual scoped tokens are enforced even if their list is empty.
        if token_scopes or current_user.get("_auth_kind") == "user_api_token":
            # Exact scope, admin, or broad action covering its resource.
            granted = token_scope_granted(token_scopes, scope)
            if not granted:
                raise HTTPException(
                    status_code=403,
                    detail=f"Insufficient token scope — '{scope}' scope required",
                )
        return current_user
    return mark_token_scope_checker(_check, scope)


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
    - Broad read/write grants its resource permissions; narrow permissions
      never grant generic or unrelated resource access. Agent scopes match
      explicitly, without an inferred hierarchy.

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
        if not token_scopes and current_user.get("_auth_kind") != "user_api_token":
            # Non-token auth (JWT, cookie, global key) — full access.
            return current_user
        if "admin" in token_scopes:
            return current_user
        for scope in allowed:
            if token_scope_granted(token_scopes, scope):
                return current_user
        raise OdinError(
            ErrorCode.scope_denied,
            f"Token scope insufficient; one of {list(allowed)} required",
            status=403,
            extra={"allowed_scopes": list(allowed), "token_scopes": list(token_scopes)},
        )

    return mark_token_scope_checker(_check, ",".join(allowed))


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
