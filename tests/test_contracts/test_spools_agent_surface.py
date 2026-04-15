"""
Contract tests — spool agent-surface retrofits (Phase 2 T2.8 + T2.9).

Two routes covered:
  - consume_spool  POST/PATCH /api/v1/spools/{id}/use
    (inventory/routes/spools.py::use_spool — dual decorator accepts both
    legacy POST and MCP's PATCH shape; body accepts either
    weight_used_g or grams)
  - assign_spool   POST /api/v1/filament-slots
    (printers/routes_filament_slots.py — new route, thin wrapper over
    FilamentSlot model to match MCP assign_spool shape)
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

SPOOLS = BACKEND_DIR / "modules" / "inventory" / "routes" / "spools.py"
FILAMENT_SLOTS = BACKEND_DIR / "modules" / "printers" / "routes_filament_slots.py"
HELPERS = BACKEND_DIR / "modules" / "inventory" / "routes" / "_helpers.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


def _assert_canonical_write(fn_src: str, handler: str):
    """Shared assertions for Phase 2 writes."""
    assert re.search(r'require_role\(\s*["\']operator["\']\s*\)', fn_src), (
        f"{handler}: require_role(operator) missing (JWT floor)."
    )
    assert re.search(
        r"require_any_scope\(\s*['\"]admin['\"]\s*,\s*AGENT_WRITE_SCOPE\s*\)",
        fn_src,
    ), f"{handler}: require_any_scope missing."
    assert "is_dry_run(request)" in fn_src, f"{handler}: no is_dry_run check."
    dry_run_pos = fn_src.index("is_dry_run(request)")
    commit_match = re.search(r"db\.(commit|add)\(", fn_src)
    assert commit_match
    assert dry_run_pos < commit_match.start(), (
        f"{handler}: is_dry_run must precede db.commit/add."
    )
    assert "dry_run_preview(" in fn_src
    assert "would_execute" in fn_src
    assert not re.search(r"HTTPException\(\s*status_code\s*=\s*4\d\d", fn_src)
    assert "next_actions" in fn_src
    assert "build_next_actions(" in fn_src


class TestConsumeSpoolSource:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(SPOOLS.read_text(), "use_spool")
        assert src, "use_spool missing from spools.py"
        return src

    def test_canonical_pattern(self, fn_src: str):
        _assert_canonical_write(fn_src, "use_spool")

    def test_accepts_both_post_and_patch(self):
        """MCP consume_spool sends PATCH; legacy portal sends POST. Both route to the same handler.

        Decorators aren't part of the ast function-def source, so search
        the whole file for both decorators preceding def use_spool.
        """
        src = SPOOLS.read_text()
        # Find the `def use_spool` line, then scan the ~200 preceding chars for decorators.
        m = re.search(r"def\s+use_spool\s*\(", src)
        assert m, "use_spool def line not found"
        prefix = src[max(0, m.start() - 400) : m.start()]
        assert re.search(r'@router\.post\(\s*["\']/\{spool_id\}/use["\']', prefix), (
            "use_spool must carry a @router.post decorator for legacy portal."
        )
        assert re.search(r'@router\.patch\(\s*["\']/\{spool_id\}/use["\']', prefix), (
            "use_spool must carry a @router.patch decorator for MCP consume_spool."
        )

    def test_odin_error_for_missing_spool(self, fn_src: str):
        assert re.search(r"OdinError\(\s*ErrorCode\.spool_not_found", fn_src)

    def test_would_execute_captures_depletion(self, fn_src: str):
        assert "would_deplete_to_empty" in fn_src or "depleted" in fn_src.lower()
        assert "new_remaining_g" in fn_src


class TestSpoolUseRequestBackwardCompat:
    """The request body shape must accept either legacy weight_used_g or MCP's grams."""

    def test_both_fields_present_in_schema(self):
        src = HELPERS.read_text()
        fn_src = _get_function_source(src, "resolved_grams") or ""
        # Verify the schema class has both fields (non-strict: defaults to None).
        assert "weight_used_g" in src
        assert "grams" in src
        # Verify the resolver property exists.
        assert "resolved_grams" in src


class TestAssignSpoolSource:
    @pytest.fixture(scope="class")
    def fn_src(self) -> str:
        src = _get_function_source(
            FILAMENT_SLOTS.read_text(), "assign_spool_to_slot"
        )
        assert src, "assign_spool_to_slot missing from routes_filament_slots.py"
        return src

    def test_canonical_pattern(self, fn_src: str):
        _assert_canonical_write(fn_src, "assign_spool_to_slot")

    def test_returns_printer_and_spool_not_found_appropriately(self, fn_src: str):
        assert re.search(r"OdinError\(\s*ErrorCode\.printer_not_found", fn_src)
        assert re.search(r"OdinError\(\s*ErrorCode\.spool_not_found", fn_src)

    def test_would_execute_includes_previous_spool(self, fn_src: str):
        """Agents need to see what was there before (for undo / audit)."""
        assert "previous_spool_id" in fn_src


class TestSpoolRoutesRegistered:
    def test_spool_use_post_and_patch_registered(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        assert ("POST", "/api/v1/spools/{spool_id}/use") in DRY_RUN_SUPPORTED_ROUTES
        assert ("PATCH", "/api/v1/spools/{spool_id}/use") in DRY_RUN_SUPPORTED_ROUTES

    def test_filament_slots_registered(self):
        from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

        assert ("POST", "/api/v1/filament-slots") in DRY_RUN_SUPPORTED_ROUTES
