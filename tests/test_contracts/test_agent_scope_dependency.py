"""
Contract test — require_any_scope() dependency (v1.8.9).

Verifies the agent:read / agent:write scope gate that protects the
MCP tool surface. Tokens are minted with these scope strings in
`api_tokens.scopes`; the dependency checks them against route-level
allow-lists.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed in test venv")
pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _invoke(allowed, current_user):
    """Run the require_any_scope dependency directly."""
    from core.rbac import require_any_scope

    dep = require_any_scope(*allowed)
    # The dep is an async function; call it with the user.
    return asyncio.run(dep(current_user=current_user))


def test_no_token_scopes_grants_full_access():
    """JWT / cookie / global-API-key sessions — no token scopes, full access."""
    user = {"id": 1, "username": "admin", "_token_scopes": []}
    result = _invoke(["admin", "agent:write"], user)
    assert result == user


def test_admin_scope_grants_any_requirement():
    user = {"id": 1, "_token_scopes": ["admin"]}
    result = _invoke(["agent:write"], user)
    assert result == user
    result2 = _invoke(["agent:read"], user)
    assert result2 == user


def test_agent_write_token_allowed_on_agent_write_route():
    from core.rbac import AGENT_WRITE_SCOPE

    user = {"id": 1, "_token_scopes": [AGENT_WRITE_SCOPE]}
    result = _invoke(["admin", AGENT_WRITE_SCOPE], user)
    assert result == user


def test_agent_read_token_denied_on_agent_write_route():
    from core.rbac import AGENT_READ_SCOPE, AGENT_WRITE_SCOPE
    from core.errors import OdinError, ErrorCode

    user = {"id": 1, "_token_scopes": [AGENT_READ_SCOPE]}
    with pytest.raises(OdinError) as exc_info:
        _invoke(["admin", AGENT_WRITE_SCOPE], user)
    assert exc_info.value.code == ErrorCode.scope_denied
    assert exc_info.value.status == 403


def test_agent_read_token_allowed_on_read_route():
    from core.rbac import AGENT_READ_SCOPE, AGENT_WRITE_SCOPE

    user = {"id": 1, "_token_scopes": [AGENT_READ_SCOPE]}
    result = _invoke(["admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE], user)
    assert result == user


def test_agent_scope_does_not_grant_legacy_write():
    """agent:write does not imply the old 'write' umbrella."""
    from core.rbac import AGENT_WRITE_SCOPE
    from core.errors import OdinError, ErrorCode

    user = {"id": 1, "_token_scopes": [AGENT_WRITE_SCOPE]}
    with pytest.raises(OdinError) as exc_info:
        _invoke(["admin", "write"], user)
    assert exc_info.value.code == ErrorCode.scope_denied


def test_legacy_write_umbrella_still_works_for_write_routes():
    """A 'write' token still grants 'write:printers' style sub-scopes."""
    user = {"id": 1, "_token_scopes": ["write:printers"]}
    result = _invoke(["admin", "write"], user)
    assert result == user


def test_unauthenticated_raises_not_authenticated():
    from core.errors import OdinError, ErrorCode

    with pytest.raises(OdinError) as exc_info:
        _invoke(["admin"], None)
    assert exc_info.value.code == ErrorCode.not_authenticated
    assert exc_info.value.status == 401


def test_wellknown_scope_constants_exist():
    from core.rbac import AGENT_READ_SCOPE, AGENT_WRITE_SCOPE

    assert AGENT_READ_SCOPE == "agent:read"
    assert AGENT_WRITE_SCOPE == "agent:write"


def test_convenience_shortcuts_return_deps():
    from core.rbac import agent_read_or_above, agent_write_or_above

    # Should return callables (FastAPI dependencies).
    dep_r = agent_read_or_above()
    dep_w = agent_write_or_above()
    assert callable(dep_r)
    assert callable(dep_w)
