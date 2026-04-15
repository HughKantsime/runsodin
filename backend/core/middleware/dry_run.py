"""`X-Dry-Run: true` header support for agent-safe previews.

Pattern:
- A client (agent or human) sends `X-Dry-Run: true` on a mutating
  request.
- Middleware sets `request.state.dry_run = True`.
- Each opted-in route handler checks the flag *before* committing DB
  writes, emitting events, or making external calls. If the flag is
  set, the handler builds a preview of what *would* happen and
  returns `{"dry_run": true, "would_execute": {...}, "next_actions": [...]}`
  with a 200 status. **No side effects.**

Why per-route opt-in instead of global rollback?
- Several ODIN routes emit async events (alert dispatch, webhook
  fanout) that run in background tasks outside the request's DB
  session. A naive `db.rollback()` after dispatching those events
  would not un-send them.
- Several routes write to the filesystem (upload endpoints) or call
  external services (SMTP test, printer MQTT). Rollback can't
  unroll those.
- Explicit per-route handling forces the author to think about
  "what changes, what's safe to preview" at each call site.

Routes that do NOT support dry-run should ignore the flag and
proceed normally. That's the safe default — dry-run is additive,
not enforced.

The list of opted-in endpoints for v1.8.9 is tracked in
`DRY_RUN_SUPPORTED_ROUTES` at the bottom of this module and echoed
in `docs/agent-surface.md` (if present).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("odin.middleware.dry_run")

# True when an incoming request has `X-Dry-Run: true` (case-insensitive
# match on the value; header name is case-insensitive per HTTP spec).
_TRUTHY_VALUES = {"true", "1", "yes", "on"}


def _parse_dry_run_header(value: Optional[str]) -> bool:
    """Return True if the header value should enable dry-run mode."""
    if not value:
        return False
    return value.strip().lower() in _TRUTHY_VALUES


async def dry_run_middleware(request: Any, call_next: Callable):
    """Set `request.state.dry_run` based on the `X-Dry-Run` header.

    The flag is only meaningful for mutating methods — on a GET it is
    still set for convenience (routes may find it useful for consistent
    behavior) but has no side effect because GETs don't commit.

    Routes that haven't been migrated to check the flag are unaffected:
    `request.state.dry_run` defaults to False if not set.
    """
    value = request.headers.get("X-Dry-Run")
    request.state.dry_run = _parse_dry_run_header(value)
    return await call_next(request)


def is_dry_run(request: Any) -> bool:
    """Check if the current request is in dry-run mode.

    Routes should call this helper (not `request.state.dry_run`
    directly) so that older Starlette versions without `state` default
    handling don't crash.
    """
    return bool(getattr(request.state, "dry_run", False))


def dry_run_preview(
    would_execute: Dict[str, Any],
    next_actions: Optional[list] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the standard dry-run response envelope.

    All agent-surface routes use this so the output shape is stable
    and the MCP/skill layer can rely on it.

    Example:
        if is_dry_run(request):
            return dry_run_preview(
                would_execute={
                    "action": "queue_job",
                    "model_id": data.model_id,
                    "printer_id": resolved_printer_id,
                    "estimated_duration_hours": estimate,
                },
                next_actions=[
                    {"tool": "list_queue", "reason": "see queue depth"},
                ],
                notes="No DB changes applied.",
            )
    """
    out: Dict[str, Any] = {
        "dry_run": True,
        "would_execute": would_execute,
    }
    if next_actions:
        out["next_actions"] = next_actions
    if notes:
        out["notes"] = notes
    return out


# ---------------------------------------------------------------------------
# Route opt-in registry
# ---------------------------------------------------------------------------

# Canonical list of routes that currently honor `X-Dry-Run: true` in
# v1.8.9. Kept here (not just in docs) so CI / contract tests can
# enumerate and assert the expected set without parsing markdown. If
# you add dry-run support to a route, add it here.
DRY_RUN_SUPPORTED_ROUTES: tuple[tuple[str, str], ...] = (
    # (method, path_template)
    ("POST", "/api/v1/queue/add"),
    ("POST", "/api/v1/jobs/{job_id}/cancel"),
    ("POST", "/api/v1/jobs/{job_id}/approve"),
    ("POST", "/api/v1/jobs/{job_id}/reject"),
    ("POST", "/api/v1/printers/{printer_id}/pause"),
    ("POST", "/api/v1/printers/{printer_id}/resume"),
    ("PATCH", "/api/v1/alerts/{alert_id}/read"),
    ("PATCH", "/api/v1/alerts/{alert_id}/dismiss"),
    ("POST", "/api/v1/spools/{spool_id}/assign"),
    ("POST", "/api/v1/spools/{spool_id}/consume"),
    ("POST", "/api/v1/maintenance/tasks/{task_id}/complete"),
)
