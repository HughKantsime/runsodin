"""
Spec AC5 — consolidated audit: every MCP-advertised route has BOTH
`require_role(...)` and `require_any_scope(...)` as dependencies.

This is the repo-wide invariant that prevents the two failure modes
the earlier plan evaluation caught:
  1. Dropping `require_role` → viewer JWTs bypass authorization
     (require_any_scope returns full access for non-token auth).
  2. Dropping `require_any_scope` → agent tokens can't use the surface.

For each of the 22 retrofitted routes, locate the handler and assert
both dependency shapes are present. Fails loud with a per-route
breakdown if any route is missing either gate.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# (kind, handler_name, file_path)
ROUTES = [
    # Writes (11)
    ("write", "pause_printer",         BACKEND_DIR / "modules" / "printers" / "routes_controls.py"),
    ("write", "resume_printer",        BACKEND_DIR / "modules" / "printers" / "routes_controls.py"),
    ("write", "mark_alert_read",       BACKEND_DIR / "modules" / "notifications" / "routes" / "alerts.py"),
    ("write", "dismiss_alert",         BACKEND_DIR / "modules" / "notifications" / "routes" / "alerts.py"),
    ("write", "create_maintenance_log", BACKEND_DIR / "modules" / "system" / "routes_maintenance.py"),
    ("write", "create_job",            BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_crud.py"),
    ("write", "cancel_job",            BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_agent.py"),
    ("write", "approve_job",           BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_agent.py"),
    ("write", "reject_job",            BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_agent.py"),
    ("write", "use_spool",             BACKEND_DIR / "modules" / "inventory" / "routes" / "spools.py"),
    ("write", "assign_spool_to_slot",  BACKEND_DIR / "modules" / "printers" / "routes_filament_slots.py"),
    # Reads (11) — farm_summary is client-composed; no backend route.
    ("read",  "list_printers",         BACKEND_DIR / "modules" / "printers" / "routes_crud.py"),
    ("read",  "get_printer",           BACKEND_DIR / "modules" / "printers" / "routes_crud.py"),
    ("read",  "list_jobs",             BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_crud.py"),
    ("read",  "get_job",               BACKEND_DIR / "modules" / "jobs" / "routes" / "jobs_crud.py"),
    ("read",  "list_alerts",           BACKEND_DIR / "modules" / "notifications" / "routes" / "alerts.py"),
    ("read",  "list_spools",           BACKEND_DIR / "modules" / "inventory" / "routes" / "spools.py"),
    ("read",  "list_filament_library", BACKEND_DIR / "modules" / "inventory" / "routes" / "filament_library.py"),
    ("read",  "list_maintenance_tasks", BACKEND_DIR / "modules" / "system" / "routes_maintenance.py"),
    ("read",  "list_orders",           BACKEND_DIR / "modules" / "orders" / "routes" / "orders_crud.py"),
]


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


def test_every_agent_route_has_require_role():
    """JWT-bypass in require_any_scope means require_role MUST be present.
    Missing require_role = viewer-JWT privilege escalation."""
    missing = []
    for kind, handler, path in ROUTES:
        src = _get_function_source(path.read_text(), handler)
        if not src:
            missing.append(f"  {kind:5s} {handler}: handler not found in {path.name}")
            continue
        if not re.search(r'require_role\(\s*["\'](?:viewer|operator|admin)["\']\s*\)', src):
            missing.append(
                f"  {kind:5s} {handler} ({path.name}): no require_role — "
                "viewer JWTs will bypass auth."
            )
    assert not missing, (
        f"{len(missing)} agent-advertised route(s) missing require_role dep:\n"
        + "\n".join(missing)
    )


def test_every_agent_route_has_require_any_scope():
    """Without require_any_scope, scoped tokens can't invoke the surface."""
    missing = []
    for kind, handler, path in ROUTES:
        src = _get_function_source(path.read_text(), handler)
        if not src:
            missing.append(f"  {kind:5s} {handler}: handler not found in {path.name}")
            continue
        if not re.search(r"require_any_scope\(", src):
            missing.append(
                f"  {kind:5s} {handler} ({path.name}): no require_any_scope — "
                "agent tokens cannot invoke this route."
            )
    assert not missing, (
        f"{len(missing)} agent-advertised route(s) missing require_any_scope dep:\n"
        + "\n".join(missing)
    )


def test_write_routes_accept_agent_write_scope():
    """Writes must admit AGENT_WRITE_SCOPE (not just read)."""
    missing = []
    for kind, handler, path in ROUTES:
        if kind != "write":
            continue
        src = _get_function_source(path.read_text(), handler)
        if not src:
            continue
        if not re.search(r"AGENT_WRITE_SCOPE", src):
            missing.append(f"  {handler} ({path.name})")
    assert not missing, (
        "Write routes must name AGENT_WRITE_SCOPE in the scope list:\n"
        + "\n".join(missing)
    )


def test_read_routes_accept_both_agent_scopes():
    """Reads must admit both AGENT_READ_SCOPE and AGENT_WRITE_SCOPE
    (write tokens naturally have strictly greater authority for reads)."""
    missing = []
    for kind, handler, path in ROUTES:
        if kind != "read":
            continue
        src = _get_function_source(path.read_text(), handler)
        if not src:
            continue
        needs_read = "AGENT_READ_SCOPE" in src
        needs_write = "AGENT_WRITE_SCOPE" in src
        if not (needs_read and needs_write):
            missing.append(
                f"  {handler} ({path.name}): read={needs_read}, write={needs_write}"
            )
    assert not missing, (
        "Read routes must admit BOTH AGENT_READ_SCOPE and AGENT_WRITE_SCOPE:\n"
        + "\n".join(missing)
    )


def test_no_write_route_missing_dry_run_branch():
    """Every retrofitted write must check is_dry_run before any mutation."""
    missing = []
    for kind, handler, path in ROUTES:
        if kind != "write":
            continue
        src = _get_function_source(path.read_text(), handler)
        if not src:
            continue
        if "is_dry_run(request)" not in src:
            missing.append(f"  {handler} ({path.name})")
    assert not missing, (
        "Write routes must call is_dry_run(request) before side effects:\n"
        + "\n".join(missing)
    )


def test_all_11_writes_in_dry_run_registry():
    """DRY_RUN_SUPPORTED_ROUTES must include an entry for every retrofitted write."""
    from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

    required_writes = {
        ("POST",  "/api/v1/printers/{printer_id}/pause"),
        ("POST",  "/api/v1/printers/{printer_id}/resume"),
        ("PATCH", "/api/v1/alerts/{alert_id}/read"),
        ("PATCH", "/api/v1/alerts/{alert_id}/dismiss"),
        ("POST",  "/api/v1/maintenance/logs"),
        ("POST",  "/api/v1/jobs"),
        ("POST",  "/api/v1/jobs/{job_id}/cancel"),
        ("POST",  "/api/v1/jobs/{job_id}/approve"),
        ("POST",  "/api/v1/jobs/{job_id}/reject"),
        ("POST",  "/api/v1/spools/{spool_id}/use"),
        ("PATCH", "/api/v1/spools/{spool_id}/use"),
        ("POST",  "/api/v1/filament-slots"),
    }
    missing = required_writes - set(DRY_RUN_SUPPORTED_ROUTES)
    assert not missing, f"Missing registry entries: {sorted(missing)}"


def test_route_count_matches_mcp_catalog():
    """The MCP v2.0.4 catalog advertises 11 writes + 11 reads = 22 tools.
    This audit covers 11 writes + 10 backend reads (+ farm_summary is
    client-composed, not a dedicated backend route)."""
    writes = [r for r in ROUTES if r[0] == "write"]
    reads = [r for r in ROUTES if r[0] == "read"]
    assert len(writes) == 11, f"Expected 11 writes, got {len(writes)}: {[h for _, h, _ in writes]}"
    assert len(reads) == 9, (
        f"Expected 9 backend reads (farm_summary + list_queue are "
        f"reusing/composing existing routes), got {len(reads)}: "
        f"{[h for _, h, _ in reads]}"
    )
