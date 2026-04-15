"""
Contract test — complete_maintenance Phase 2 agent-surface retrofit (T2.10).

POST /api/v1/maintenance/logs follows the canonical write pattern.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed")
pytest.importorskip("fastapi", reason="FastAPI not installed")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

ROUTES_MAINT = BACKEND_DIR / "modules" / "system" / "routes_maintenance.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestCompleteMaintenanceSourceContract:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(ROUTES_MAINT.read_text(), "create_maintenance_log")
        assert src, "create_maintenance_log missing from routes_maintenance.py"
        return src

    def test_stacks_role_and_agent_scope(self, fn_src: str):
        assert re.search(r'require_role\(\s*["\']operator["\']\s*\)', fn_src)
        assert re.search(
            r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
            fn_src,
        )

    def test_checks_dry_run_before_any_side_effect(self, fn_src: str):
        assert "is_dry_run(request)" in fn_src
        dry_run_pos = fn_src.index("is_dry_run(request)")
        commit_match = re.search(r"db\.commit\(", fn_src)
        db_add_match = re.search(r"db\.add\(", fn_src)
        assert commit_match and db_add_match
        assert dry_run_pos < db_add_match.start(), (
            "is_dry_run must precede db.add so dry-run does not create the log row."
        )

    def test_dry_run_preview_shape(self, fn_src: str):
        assert "dry_run_preview(" in fn_src
        assert "would_execute" in fn_src
        # Maintenance-specific: record the hours-at-service that would be computed
        assert "print_hours_at_service" in fn_src

    def test_odin_error_for_missing_printer(self, fn_src: str):
        assert re.search(r"OdinError\(\s*ErrorCode\.printer_not_found", fn_src)
        assert not re.search(r"HTTPException\(\s*status_code\s*=\s*4\d\d", fn_src)

    def test_success_emits_next_actions(self, fn_src: str):
        assert "build_next_actions(" in fn_src
        assert "list_maintenance_tasks" in fn_src


class TestRegistered:
    def test_in_supported_routes(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        assert ("POST", "/api/v1/maintenance/logs") in DRY_RUN_SUPPORTED_ROUTES
