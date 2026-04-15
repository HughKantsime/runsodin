"""
Contract test — agent-surface reads (Phase 3 T3.1-T3.11).

Eleven read routes the MCP v2.0.4+ advertises. Each must:
  1. Keep existing `require_role(...)` (JWT floor).
  2. Add `Depends(require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE))`
     so agent:read and agent:write tokens can invoke reads.
  3. No dry-run or next_actions — reads are side-effect-free.

Shared source-level assertion. Handler-by-handler coverage; one
registry-style test per file as well.

Note — farm_summary is composed client-side (odin-mcp/src/tools/read_tools.ts:316
fans out over /printers, /jobs, /alerts). No dedicated backend route —
coverage comes transitively through list_printers / list_jobs / list_alerts.
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

# (handler_name, file_path)
READ_HANDLERS = [
    ("list_printers",          BACKEND_DIR / "modules" / "printers" / "routes_crud.py"),
    ("get_printer",            BACKEND_DIR / "modules" / "printers" / "routes_crud.py"),
    ("list_jobs",              BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_crud.py"),
    ("get_job",                BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_crud.py"),
    ("list_alerts",            BACKEND_DIR / "modules" / "notifications" / "routes" / "alerts.py"),
    ("list_spools",            BACKEND_DIR / "modules" / "inventory" / "routes" / "spools.py"),
    ("list_filament_library",  BACKEND_DIR / "modules" / "inventory" / "routes" / "filament_library.py"),
    ("list_maintenance_tasks", BACKEND_DIR / "modules" / "system" / "routes_maintenance.py"),
    ("list_orders",            BACKEND_DIR / "modules" / "orders" / "routes" / "orders_crud.py"),
]


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


@pytest.mark.parametrize("handler,path", READ_HANDLERS)
def test_read_handler_has_stacked_auth(handler: str, path: Path):
    """Every MCP-advertised read has BOTH require_role + require_any_scope."""
    src = _get_function_source(path.read_text(), handler)
    assert src, f"{handler} missing from {path.name}"

    assert re.search(r'require_role\(\s*["\']viewer["\']\s*\)', src), (
        f"{handler} in {path.name} must keep require_role('viewer') as JWT floor."
    )
    assert re.search(
        r"require_any_scope\([^)]*AGENT_READ_SCOPE",
        src,
    ), (
        f"{handler} in {path.name} must add require_any_scope with AGENT_READ_SCOPE "
        "so agent:read tokens are admitted."
    )
    assert re.search(
        r"AGENT_WRITE_SCOPE",
        src,
    ), (
        f"{handler} in {path.name}: AGENT_WRITE_SCOPE must also be accepted "
        "(agent:write is a superset of agent:read for reads)."
    )


@pytest.mark.parametrize("handler,path", READ_HANDLERS)
def test_read_handler_has_no_dry_run_branch(handler: str, path: Path):
    """Reads should NOT call is_dry_run — there are no side effects to preview."""
    src = _get_function_source(path.read_text(), handler)
    assert src
    # is_dry_run on a read is a code smell — indicates someone mistook a
    # read for a write. Fail loud so reviewer sees it.
    assert "is_dry_run(request)" not in src, (
        f"{handler} in {path.name} should not call is_dry_run — reads have no side effects."
    )


class TestFilamentLibraryAlias:
    """list_filaments (MCP) maps to a NEW backend route /filament-library (M3 resolution)."""

    def test_filament_library_route_exists(self):
        path = BACKEND_DIR / "modules" / "inventory" / "routes" / "filament_library.py"
        assert path.exists(), (
            "filament_library.py must exist as the MCP-compat alias for "
            "list_filaments (path /filament-library)."
        )
        src = path.read_text()
        # Correct prefix on the router.
        assert re.search(
            r'router\s*=\s*APIRouter\(\s*prefix\s*=\s*["\']\/filament-library["\']',
            src,
        ), "Router prefix must be /filament-library to match MCP list_filaments."

    def test_filament_library_registered_in_inventory_router(self):
        init_src = (BACKEND_DIR / "modules" / "inventory" / "routes" / "__init__.py").read_text()
        assert "filament_library_router" in init_src, (
            "filament_library_router must be imported and included in "
            "inventory/routes/__init__.py."
        )
