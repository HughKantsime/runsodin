"""
Contract test — pause_printer Phase 2 agent-surface retrofit (v1.8.9).

Guards the per-route retrofit pattern from regression. The route
`POST /api/v1/printers/{printer_id}/pause` must:

  1. Accept both JWT operator role (backward compat) AND
     `agent:write`-scoped tokens (new agent surface).
  2. Honor `X-Dry-Run: true` by returning the standard
     `dry_run_preview(...)` envelope *before* any MQTT send or DB
     commit.
  3. Use `OdinError` with `ErrorCode.printer_not_found` for missing
     printers (legacy `HTTPException(404, ...)` is forbidden here).
  4. Emit `next_actions` on the success response so agents can chain.
  5. Be registered in `DRY_RUN_SUPPORTED_ROUTES`.

This is a source-level contract test (matches
`test_job_complete_idempotency.py` style) so it runs fast without
spinning up the app. A companion runtime test lives in
`tests/test_e2e/test_agent_surface_writes.py` once the live harness
is up.

Why source-level: a future refactor that quietly removes the
`is_dry_run(request)` branch — or swaps `OdinError` back to
`HTTPException` — would regress the agent contract without failing
any other test in the suite. This test fails loudly at that point.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed in test venv")
pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

ROUTES_CONTROLS = BACKEND_DIR / "modules" / "printers" / "routes_controls.py"
DRY_RUN_MODULE = BACKEND_DIR / "core" / "middleware" / "dry_run.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


# ---------------------------------------------------------------------------
# Source-level: pause_printer handler shape
# ---------------------------------------------------------------------------


class TestPausePrinterSourceContract:
    """pause_printer() must implement the Phase 2 agent-surface pattern."""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        return ROUTES_CONTROLS.read_text()

    @pytest.fixture(scope="class")
    def fn_src(self, source: str) -> str:
        src = _get_function_source(source, "pause_printer")
        assert src, "pause_printer is missing from routes_controls.py"
        return src

    def test_stacks_role_and_agent_scope_dependencies(self, fn_src: str):
        """Both require_role (JWT compat) AND require_any_scope (agent tokens)."""
        assert re.search(r'require_role\(\s*["\']operator["\']\s*\)', fn_src), (
            "pause_printer must keep require_role('operator') so JWT operator "
            "sessions continue to authorize (backward-compat with portal UI)."
        )
        assert re.search(
            r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
            fn_src,
        ), (
            "pause_printer must add require_any_scope('admin', AGENT_WRITE_SCOPE) "
            "so agent:write-scoped tokens can invoke the route. Stacking both "
            "gates preserves JWT RBAC while enabling scoped-token auth."
        )

    def test_checks_dry_run_before_any_side_effect(self, fn_src: str):
        """is_dry_run(request) branch must precede _send_printer_command and db.commit."""
        assert "is_dry_run(request)" in fn_src, (
            "pause_printer must call is_dry_run(request) to honor the X-Dry-Run header."
        )

        dry_run_pos = fn_src.index("is_dry_run(request)")
        send_cmd_match = re.search(r"_send_printer_command\(", fn_src)
        commit_match = re.search(r"db\.commit\(", fn_src)

        assert send_cmd_match, "pause_printer must still call _send_printer_command on the real path."
        assert commit_match, "pause_printer must still commit the DB update on the real path."

        assert dry_run_pos < send_cmd_match.start(), (
            "is_dry_run() check must appear BEFORE _send_printer_command() — "
            "otherwise a dry-run request sends the real MQTT pause."
        )
        assert dry_run_pos < commit_match.start(), (
            "is_dry_run() check must appear BEFORE db.commit() — "
            "otherwise a dry-run request mutates the printers table."
        )

    def test_dry_run_branch_returns_preview_envelope(self, fn_src: str):
        """The dry_run branch must return dry_run_preview(would_execute=...)."""
        assert "dry_run_preview(" in fn_src, (
            "dry_run branch must return dry_run_preview(...) — never build an "
            "ad-hoc envelope. Stable shape is what the MCP client depends on."
        )
        assert "would_execute" in fn_src, (
            "dry_run_preview must be called with a would_execute kwarg describing "
            "what the real call would do."
        )

    def test_uses_odin_error_not_httpexception_for_404(self, fn_src: str):
        """Missing printer must raise OdinError(printer_not_found), not HTTPException."""
        assert re.search(
            r"OdinError\(\s*ErrorCode\.printer_not_found",
            fn_src,
        ), (
            "pause_printer must raise OdinError(ErrorCode.printer_not_found, ...) "
            "on missing/unauthorized printer so the agent error envelope is emitted."
        )
        # HTTPException(status_code=404, ...) is banned in this handler.
        assert not re.search(
            r"HTTPException\(\s*status_code\s*=\s*404",
            fn_src,
        ), (
            "pause_printer no longer uses HTTPException for 404 — regression. "
            "Use OdinError(ErrorCode.printer_not_found, ...) instead."
        )

    def test_success_response_emits_next_actions(self, fn_src: str):
        """Success path must include next_actions for agent chaining."""
        assert "next_actions" in fn_src, (
            "pause_printer success response must include a next_actions list "
            "so downstream agents can chain (e.g. get_printer to verify)."
        )
        assert "build_next_actions(" in fn_src or "next_action(" in fn_src, (
            "Use the build_next_actions / next_action helpers rather than "
            "constructing dicts by hand — guarantees the documented shape."
        )

    def test_upstream_failure_is_retriable(self, fn_src: str):
        """503 on MQTT send failure must be marked retriable=True so agents back off and retry."""
        # Either ErrorCode.upstream_unavailable (in _RETRIABLE_CODES by default)
        # or an explicit retriable=True flag.
        has_upstream = "ErrorCode.upstream_unavailable" in fn_src
        has_explicit = re.search(r"retriable\s*=\s*True", fn_src)
        assert has_upstream or has_explicit, (
            "Printer-unreachable path must be retriable — either raise "
            "OdinError(ErrorCode.upstream_unavailable, ...) or set "
            "retriable=True explicitly so clients know to back off and retry."
        )

    def test_stacked_auth_rationale_comment_present(self, fn_src: str):
        """A future refactor that deletes require_role("operator") as "unused"
        would reintroduce the viewer-JWT-escalation bug. The inline rationale
        comment above the two Depends() lines is the source-of-record for
        why both exist. This test enforces that comment is present — so a
        naive cleanup refactor fails here before it can regress the auth shape.
        """
        # Comment must reference the core/rbac.py:193-196 bypass behavior,
        # or at least clearly document the JWT vs token-scope rationale.
        mentions_jwt_bypass = (
            "JWT" in fn_src
            or "rbac.py" in fn_src
            or "non-token auth" in fn_src
        )
        assert mentions_jwt_bypass, (
            "pause_printer must carry an inline comment explaining why both "
            "require_role and require_any_scope are stacked. Reference "
            "core/rbac.py:193-196 (the JWT bypass line) so future refactors "
            "know not to drop the role dep."
        )
        # Must explicitly instruct future maintainers not to simplify.
        forbids_simplification = re.search(
            r"(BOTH must be present|do not.*simplify|do not.*drop|do not.*remove)",
            fn_src,
            re.IGNORECASE,
        )
        assert forbids_simplification, (
            "Rationale comment must contain explicit instruction not to drop "
            "either dep. Phrase like 'BOTH must be present' or 'do not simplify' "
            "so grep-based code-review hits it."
        )


# ---------------------------------------------------------------------------
# Registry: pause_printer must be in the supported-routes tuple
# ---------------------------------------------------------------------------


class TestPausePrinterRegistered:
    def test_pause_printer_in_supported_routes(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        entry = ("POST", "/api/v1/printers/{printer_id}/pause")
        assert entry in DRY_RUN_SUPPORTED_ROUTES, (
            f"pause_printer must be registered in DRY_RUN_SUPPORTED_ROUTES. "
            f"Current tuple: {DRY_RUN_SUPPORTED_ROUTES}"
        )


# ---------------------------------------------------------------------------
# Shape smoke: dry_run_preview envelope matches MCP client expectation
# ---------------------------------------------------------------------------


class TestDryRunPreviewShape:
    """The envelope shape pause_printer returns must match what the MCP client parses."""

    def test_envelope_has_required_keys(self):
        from core.middleware.dry_run import dry_run_preview

        out = dry_run_preview(
            would_execute={"action": "pause_print", "printer_id": 1},
            next_actions=[{"tool": "get_printer", "args": {"printer_id": 1}}],
            notes="test",
        )
        assert out["dry_run"] is True
        assert out["would_execute"]["action"] == "pause_print"
        assert isinstance(out["next_actions"], list)
        assert out["notes"] == "test"

    def test_envelope_omits_optional_keys_when_not_provided(self):
        from core.middleware.dry_run import dry_run_preview

        out = dry_run_preview(would_execute={"x": 1})
        assert out["dry_run"] is True
        assert "next_actions" not in out
        assert "notes" not in out
