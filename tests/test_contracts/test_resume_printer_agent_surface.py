"""
Contract test — resume_printer Phase 2 agent-surface retrofit (v1.9.0).

Mirror of test_pause_printer_agent_surface.py for `POST /api/v1/printers/{id}/resume`.
Same 7 criteria from spec R6; same source-level assertion style.
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


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestResumePrinterSourceContract:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(ROUTES_CONTROLS.read_text(), "resume_printer")
        assert src, "resume_printer missing from routes_controls.py"
        return src

    def test_stacks_role_and_agent_scope_dependencies(self, fn_src: str):
        assert re.search(r'require_role\(\s*["\']operator["\']\s*\)', fn_src), (
            "resume_printer must keep require_role('operator') — portal UI operator "
            "JWT sessions depend on it (require_any_scope bypasses JWT entirely)."
        )
        assert re.search(
            r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
            fn_src,
        ), "resume_printer must add the agent:write scope gate."

    def test_checks_dry_run_before_any_side_effect(self, fn_src: str):
        assert "is_dry_run(request)" in fn_src
        dry_run_pos = fn_src.index("is_dry_run(request)")
        send_cmd_match = re.search(r"_send_printer_command\(", fn_src)
        commit_match = re.search(r"db\.commit\(", fn_src)
        assert send_cmd_match and commit_match
        assert dry_run_pos < send_cmd_match.start(), (
            "is_dry_run check must precede _send_printer_command so dry-run "
            "does not emit MQTT."
        )
        assert dry_run_pos < commit_match.start(), (
            "is_dry_run check must precede db.commit so dry-run does not mutate DB."
        )

    def test_dry_run_branch_returns_preview_envelope(self, fn_src: str):
        assert "dry_run_preview(" in fn_src
        assert "would_execute" in fn_src
        # Specific to resume: target state is RUNNING.
        assert "RUNNING" in fn_src, "resume target_gcode_state should be RUNNING"

    def test_uses_odin_error_not_httpexception(self, fn_src: str):
        assert re.search(
            r"OdinError\(\s*ErrorCode\.printer_not_found", fn_src
        ), "Missing-printer path must use OdinError(printer_not_found)."
        assert not re.search(
            r"HTTPException\(\s*status_code\s*=\s*4\d\d", fn_src
        ), "No HTTPException(4xx) allowed in retrofitted resume_printer."

    def test_success_response_emits_next_actions(self, fn_src: str):
        assert "next_actions" in fn_src
        assert "build_next_actions(" in fn_src

    def test_upstream_failure_is_retriable(self, fn_src: str):
        has_upstream = "ErrorCode.upstream_unavailable" in fn_src
        has_explicit = re.search(r"retriable\s*=\s*True", fn_src)
        assert has_upstream or has_explicit


class TestResumePrinterRegistered:
    def test_resume_printer_in_supported_routes(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        entry = ("POST", "/api/v1/printers/{printer_id}/resume")
        assert entry in DRY_RUN_SUPPORTED_ROUTES, (
            f"resume_printer must be in DRY_RUN_SUPPORTED_ROUTES. "
            f"Current: {DRY_RUN_SUPPORTED_ROUTES}"
        )
