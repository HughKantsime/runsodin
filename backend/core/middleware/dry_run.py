"""`X-Dry-Run: true` header support for agent-safe previews.

v1.8.9 status (post-codex-fix-3, 2026-04-14):
  **Phase 1 ships the infrastructure only.** The middleware sets
  `request.state.dry_run` from the header and the `dry_run_preview()`
  helper builds the canonical response envelope. **Zero routes
  currently opt in**, so the header is inert today — it does not
  cause any route to skip its side effects.

  Per-route opt-in is Phase 2 work, paired one-to-one with the MCP
  tool that exercises each route. A route is considered "opted in"
  only when its handler:
    1. Reads `is_dry_run(request)` before any DB commit / event emit
       / filesystem write / external call, AND
    2. Returns `dry_run_preview(...)` on that branch.

  Until a route opts in, sending `X-Dry-Run: true` to that route will
  execute normally. Clients must NOT assume dry-run works against a
  given endpoint without verifying the route explicitly supports it.

Pattern:
- A client (agent or human) sends `X-Dry-Run: true` on a mutating
  request.
- Middleware sets `request.state.dry_run = True`.
- Each opted-in route handler checks the flag *before* committing DB
  writes, emitting events, or making external calls. If the flag is
  set, the handler builds a preview of what *would* happen and
  returns `{"dry_run": true, "would_execute": {...}, "next_actions": [...]}`
  with a 200 status. **No side effects on that branch.**

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

Routes that do NOT support dry-run ignore the flag and proceed
normally. That's the safe default for an infrastructure-only Phase 1
release — dry-run is additive, not enforced.
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
#
# Phase 1 ships with zero opted-in routes. The empty tuple is
# intentional — the previous draft listed 11 endpoints as a roadmap,
# but codex adversarial review (2026-04-14) correctly flagged that
# listing routes here without corresponding `is_dry_run(request)`
# branches was a dangerous contract bug: clients would read the
# registry, send `X-Dry-Run: true`, and the route would still
# perform the real mutation.
#
# Phase 2 populates this tuple one entry at a time, in lockstep with
# the corresponding route retrofit:
#     1. Route handler reads `is_dry_run(request)` and returns
#        `dry_run_preview(...)` before any DB commit / event emit.
#     2. Route is added to this tuple.
#     3. A contract test asserts the `dry_run: true` path produces
#        no DB side effects for that route.
# All three must land in the same commit.
#
# Contract test `test_dry_run_middleware.py::test_supported_routes_enumeration_shape`
# asserts the tuple shape but does NOT assert its length — the tuple
# is allowed to be empty while route migration is in flight.
DRY_RUN_SUPPORTED_ROUTES: tuple[tuple[str, str], ...] = ()
