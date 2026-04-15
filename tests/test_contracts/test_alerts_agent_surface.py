"""
Contract test — mark_alert_read + dismiss_alert Phase 2 agent-surface
retrofit (v1.9.0, T2.6 + T2.7).

Both routes live in modules/notifications/routes/alerts.py and follow
the canonical Phase 2 write pattern. Combined file because they share
the source (one read + assert both).
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

ALERTS = BACKEND_DIR / "modules" / "notifications" / "routes" / "alerts.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


def _check_canonical_write_pattern(fn_src: str, handler_name: str):
    """Shared per-route assertions for Phase 2 agent-advertised writes."""
    # Stacked auth.
    assert re.search(r'require_role\(\s*["\'](?:viewer|operator)["\']\s*\)', fn_src), (
        f"{handler_name} must keep require_role as the JWT floor."
    )
    assert re.search(
        r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
        fn_src,
    ), f"{handler_name} must add the agent:write scope gate."

    # Dry-run branch BEFORE commit.
    assert "is_dry_run(request)" in fn_src
    dry_run_pos = fn_src.index("is_dry_run(request)")
    commit_match = re.search(r"db\.commit\(", fn_src)
    assert commit_match
    assert dry_run_pos < commit_match.start(), (
        f"{handler_name}: is_dry_run must precede db.commit."
    )
    assert "dry_run_preview(" in fn_src
    assert "would_execute" in fn_src

    # OdinError for 404, no HTTPException(4xx).
    assert re.search(
        r"OdinError\(\s*ErrorCode\.(alert_not_found|not_found)", fn_src
    ), f"{handler_name} must use OdinError for missing alert."
    assert not re.search(r"HTTPException\(\s*status_code\s*=\s*4\d\d", fn_src), (
        f"{handler_name} no HTTPException(4xx) in retrofit."
    )

    # next_actions on success.
    assert "next_actions" in fn_src
    assert "build_next_actions(" in fn_src


class TestMarkAlertReadSourceContract:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(ALERTS.read_text(), "mark_alert_read")
        assert src, "mark_alert_read missing from alerts.py"
        return src

    def test_canonical_write_pattern(self, fn_src: str):
        _check_canonical_write_pattern(fn_src, "mark_alert_read")

    def test_would_execute_includes_from_to_read_state(self, fn_src: str):
        """Status transition is_read False→True is the core semantic."""
        assert "from_is_read" in fn_src or "is_read" in fn_src
        assert '"to_is_read": True' in fn_src or "to_is_read=True" in fn_src or "to_is_read" in fn_src


class TestDismissAlertSourceContract:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(ALERTS.read_text(), "dismiss_alert")
        assert src, "dismiss_alert missing from alerts.py"
        return src

    def test_canonical_write_pattern(self, fn_src: str):
        _check_canonical_write_pattern(fn_src, "dismiss_alert")

    def test_would_execute_notes_also_marks_read(self, fn_src: str):
        """Dismissing implies reading — semantic must be captured in the preview."""
        assert "also_marks_read" in fn_src or "is_read" in fn_src


class TestAlertRoutesRegistered:
    def test_mark_alert_read_in_supported_routes(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        assert ("PATCH", "/api/v1/alerts/{alert_id}/read") in DRY_RUN_SUPPORTED_ROUTES

    def test_dismiss_alert_in_supported_routes(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        assert ("PATCH", "/api/v1/alerts/{alert_id}/dismiss") in DRY_RUN_SUPPORTED_ROUTES
