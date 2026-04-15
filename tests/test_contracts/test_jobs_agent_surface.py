"""
Contract tests — jobs agent-surface retrofits (Phase 2 T2.1-T2.4).

Four routes covered:
  - create_job (queue_job)  — POST /api/v1/jobs
  - cancel_job              — POST /api/v1/jobs/{id}/cancel
  - approve_job             — POST /api/v1/jobs/{id}/approve
  - reject_job              — POST /api/v1/jobs/{id}/reject

All follow the canonical Phase 2 write pattern. Source-level AST/regex
assertions per handler + registry check per route.
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

JOBS_CRUD = BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_crud.py"
JOBS_AGENT = BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_agent.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


def _assert_canonical_write(fn_src: str, handler: str):
    """Shared assertions for Phase 2 agent-advertised writes."""
    # Role floor (viewer OK for queue_job since it can submit-for-approval; operator for others).
    assert re.search(r'require_role\(\s*["\'](?:viewer|operator)["\']\s*\)', fn_src), (
        f"{handler}: require_role(viewer|operator) missing (JWT floor)."
    )
    # Agent-write scope gate.
    assert re.search(
        r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
        fn_src,
    ), f"{handler}: require_any_scope(admin, AGENT_WRITE_SCOPE) missing."

    # Dry-run branch BEFORE commit.
    assert "is_dry_run(request)" in fn_src, f"{handler}: no is_dry_run check."
    dry_run_pos = fn_src.index("is_dry_run(request)")
    commit_match = re.search(r"db\.(commit|add)\(", fn_src)
    assert commit_match, f"{handler}: no db.commit/add call."
    assert dry_run_pos < commit_match.start(), (
        f"{handler}: is_dry_run must precede db.commit/add."
    )
    assert "dry_run_preview(" in fn_src
    assert "would_execute" in fn_src

    # OdinError for 4xx, not HTTPException.
    assert re.search(
        r"OdinError\(\s*ErrorCode\.(job_not_found|printer_not_found|invalid_state_transition|validation_failed|quota_exceeded)",
        fn_src,
    ), f"{handler}: expected OdinError for 4xx paths, none found."
    assert not re.search(r"HTTPException\(\s*status_code\s*=\s*4\d\d", fn_src), (
        f"{handler}: HTTPException(4xx) forbidden in retrofit."
    )

    # next_actions in success path.
    assert "next_actions" in fn_src
    assert "build_next_actions(" in fn_src


class TestQueueJobSource:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(JOBS_CRUD.read_text(), "create_job")
        assert src, "create_job missing from jobs_crud.py"
        return src

    def test_canonical_pattern(self, fn_src: str):
        _assert_canonical_write(fn_src, "create_job")

    def test_quota_exceeded_uses_odin_error(self, fn_src: str):
        """Quota exceeded is 429, must use OdinError(quota_exceeded) not HTTPException(429)."""
        assert re.search(r"OdinError\(\s*ErrorCode\.quota_exceeded", fn_src)
        assert not re.search(r"HTTPException\(\s*status_code\s*=\s*429", fn_src)

    def test_would_execute_includes_approval_flag(self, fn_src: str):
        assert "requires_approval" in fn_src or "initial_status" in fn_src


class TestCancelJobSource:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(JOBS_AGENT.read_text(), "cancel_job")
        assert src, "cancel_job missing from jobs_lifecycle.py"
        return src

    def test_canonical_pattern(self, fn_src: str):
        _assert_canonical_write(fn_src, "cancel_job")

    def test_would_execute_includes_state_transition(self, fn_src: str):
        assert "from_status" in fn_src and "to_status" in fn_src
        assert "cancelled" in fn_src


class TestApproveJobSource:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(JOBS_AGENT.read_text(), "approve_job")
        assert src, "approve_job missing from jobs_lifecycle.py"
        return src

    def test_canonical_pattern(self, fn_src: str):
        _assert_canonical_write(fn_src, "approve_job")

    def test_would_execute_captures_alert_dispatch_intent(self, fn_src: str):
        """Approve dispatches JOB_APPROVED alert — the dry-run preview should note this."""
        assert "would_notify_submitter" in fn_src or "dispatch" in fn_src.lower()


class TestRejectJobSource:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(JOBS_AGENT.read_text(), "reject_job")
        assert src, "reject_job missing from jobs_lifecycle.py"
        return src

    def test_canonical_pattern(self, fn_src: str):
        _assert_canonical_write(fn_src, "reject_job")

    def test_validation_failure_on_missing_reason_uses_odin_error(self, fn_src: str):
        assert re.search(r"OdinError\(\s*ErrorCode\.validation_failed", fn_src)

    def test_would_execute_includes_reason(self, fn_src: str):
        assert '"reason"' in fn_src or "reason=" in fn_src


class TestJobRoutesRegistered:
    def test_all_four_in_supported_routes(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        required = {
            ("POST", "/api/v1/jobs"),
            ("POST", "/api/v1/jobs/{job_id}/cancel"),
            ("POST", "/api/v1/jobs/{job_id}/approve"),
            ("POST", "/api/v1/jobs/{job_id}/reject"),
        }
        missing = required - set(DRY_RUN_SUPPORTED_ROUTES)
        assert not missing, f"Missing jobs-route registrations: {missing}"
