"""
Contract test — agent-scoped token minting (Phase 2, R2).

POST /tokens (create_api_token) must:
  1. Accept "agent:read" and "agent:write" as valid scopes
     (VALID_SCOPES allowlist expanded).
  2. Enforce a mint-time role floor: only operator+ can mint
     agent:write. Viewers are allowed to mint agent:read.
  3. Raise OdinError(permission_denied, status=403) when a viewer
     requests agent:write — structured envelope, not bare
     HTTPException, so agents branch on err.code cleanly.

Why the role floor matters: `require_any_scope` (the route-level
gate for agent scopes) does not check the user's role — it bypasses
scope enforcement entirely for JWT sessions and admits any token
whose scopes match. So the only place the role floor is enforced for
agent:write is at token-mint time. If a viewer could mint
agent:write, they would escape the operator-role intent of the
write routes entirely.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

ROUTES_SESSIONS = BACKEND_DIR / "modules" / "organizations" / "routes_sessions.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


def _extract_valid_scopes(source: str) -> set[str]:
    """Parse `VALID_SCOPES = { ... }` out of routes_sessions.py source.

    Source-level extraction so this test doesn't trigger the module's
    full import chain (which pulls in slowapi and other runtime deps
    absent from the contract-test venv).
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "VALID_SCOPES":
                    value = node.value
                    if isinstance(value, ast.Set):
                        return {
                            el.value for el in value.elts
                            if isinstance(el, ast.Constant) and isinstance(el.value, str)
                        }
    return set()


class TestValidScopesIncludesAgent:
    @pytest.fixture(scope="class")
    def scopes(self) -> set[str]:
        return _extract_valid_scopes(ROUTES_SESSIONS.read_text())

    def test_agent_read_in_allowlist(self, scopes: set[str]):
        assert "agent:read" in scopes, (
            f"agent:read missing from VALID_SCOPES. Current: {sorted(scopes)}"
        )

    def test_agent_write_in_allowlist(self, scopes: set[str]):
        assert "agent:write" in scopes, (
            f"agent:write missing from VALID_SCOPES. Current: {sorted(scopes)}"
        )

    def test_legacy_scopes_still_present(self, scopes: set[str]):
        """Adding agent:* must not silently remove existing scopes."""
        for required in ("read", "write", "admin", "read:printers", "write:printers"):
            assert required in scopes, f"legacy scope {required} missing"


# ---------------------------------------------------------------------------
# Source-level: mint-time role floor
# ---------------------------------------------------------------------------


class TestMintRoleFloor:
    """The create_api_token function must reject agent:write from non-operator roles."""

    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(ROUTES_SESSIONS.read_text(), "create_api_token")
        assert src, "create_api_token missing from routes_sessions.py"
        return src

    def test_agent_write_checked_against_role(self, fn_src: str):
        """Must have a guard like: if 'agent:write' in scopes and role not in ('admin','operator')."""
        pattern = re.compile(
            r'["\']agent:write["\']\s*in\s*scopes\b',
            re.MULTILINE | re.DOTALL,
        )
        assert pattern.search(fn_src), (
            "create_api_token must include a check: "
            "if 'agent:write' in scopes. Without this, viewers could "
            "mint agent:write and bypass the operator-role intent."
        )

    def test_agent_write_role_list_includes_admin_and_operator(self, fn_src: str):
        """The role floor must permit both operator and admin."""
        # Find the actual conditional line — the `if "agent:write" in scopes ...`
        # statement, not a comment that mentions the string.
        if_stmt = re.search(
            r'if\s+["\']agent:write["\']\s+in\s+scopes\b[^\n]*',
            fn_src,
        )
        assert if_stmt, "Could not locate the `if 'agent:write' in scopes` guard line"
        # The role check may span multiple lines; grab 800 chars from the if stmt
        # to catch the full block including the raise.
        start = if_stmt.start()
        block = fn_src[start : start + 800]
        assert "operator" in block and "admin" in block, (
            "Role floor for agent:write mint must admit both 'admin' and 'operator'. "
            f"Block parsed:\n{block}"
        )

    def test_raises_odin_error_not_httpexception(self, fn_src: str):
        """Fail path for agent:write role-mismatch must be OdinError (envelope)."""
        if_stmt = re.search(
            r'if\s+["\']agent:write["\']\s+in\s+scopes\b[^\n]*',
            fn_src,
        )
        assert if_stmt, "Could not locate the `if 'agent:write' in scopes` guard line"
        start = if_stmt.start()
        block = fn_src[start : start + 800]
        assert re.search(
            r"OdinError\(\s*ErrorCode\.permission_denied",
            block,
        ), (
            "agent:write mint failure must raise OdinError(permission_denied) "
            "so MCP clients see the structured error envelope. HTTPException(403) "
            f"is insufficient. Block parsed:\n{block}"
        )


# ---------------------------------------------------------------------------
# Agent-read is NOT gated beyond viewer (the route's own floor)
# ---------------------------------------------------------------------------


class TestAgentReadNotOverGated:
    def test_agent_read_not_role_gated_at_mint(self):
        """Viewers must be able to mint agent:read without hitting a role floor.

        agent:read grants the 11 advertised read tools. Read routes
        already floor on viewer (the lowest authenticated role) — so
        requiring operator+ at mint time would be strictly less
        permissive than request time, which is nonsensical.
        """
        fn_src = _get_function_source(ROUTES_SESSIONS.read_text(), "create_api_token")
        # Look for any `agent:read` check that raises 403/OdinError.
        # There should be NONE.
        bad_patterns = [
            re.compile(r'["\']agent:read["\']\s*in\s*scopes[\s\S]{0,200}?raise'),
            re.compile(r'if\s*["\']agent:read["\'][\s\S]{0,200}?permission_denied'),
        ]
        for p in bad_patterns:
            assert not p.search(fn_src), (
                "create_api_token should NOT role-gate agent:read at mint. "
                f"Found matching pattern: {p.pattern}"
            )
