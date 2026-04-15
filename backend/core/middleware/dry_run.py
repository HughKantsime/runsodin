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
import re
from typing import Any, Callable, Dict, Optional, Pattern

from starlette.responses import JSONResponse

log = logging.getLogger("odin.middleware.dry_run")

# True when an incoming request has `X-Dry-Run: true` (case-insensitive
# match on the value; header name is case-insensitive per HTTP spec).
_TRUTHY_VALUES = {"true", "1", "yes", "on"}

# Methods that can have server-side side effects and therefore need the
# dry-run gate. GET/HEAD/OPTIONS are excluded — they have no side effect
# by HTTP contract, so `X-Dry-Run` on them is a no-op (not an error).
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _parse_dry_run_header(value: Optional[str]) -> bool:
    """Return True if the header value should enable dry-run mode."""
    if not value:
        return False
    return value.strip().lower() in _TRUTHY_VALUES


def _compile_route_template(template: str) -> Pattern[str]:
    """Compile a FastAPI path template to a regex matching real request paths.

    `{param}` segments match exactly one non-slash segment. The pattern is
    anchored at both ends so partial matches do not slip through (e.g.
    `/api/v1/printers/42/logs` must NOT match
    `/api/v1/printers/{printer_id}/pause`).

    Implementation note: `re.escape` turns `{` into `\\{` in the output,
    so we match `\\{...\\}` (escaped braces) rather than raw `{...}`.
    """
    escaped = re.escape(template)
    pattern = re.sub(r"\\\{[^/}]+\\\}", r"[^/]+", escaped)
    return re.compile(f"^{pattern}$")


def _build_supported_matcher() -> list[tuple[str, Pattern[str]]]:
    """Compile every DRY_RUN_SUPPORTED_ROUTES entry into a (method, regex) pair.

    Called once at module load. The middleware hot path iterates this list;
    small (single-digit to low-double-digit size) so linear scan is fine.
    """
    return [(method, _compile_route_template(path)) for method, path in DRY_RUN_SUPPORTED_ROUTES]


async def dry_run_middleware(request: Any, call_next: Callable):
    """Parse `X-Dry-Run` header, deny-by-default on unsupported routes.

    Behavior:
      - Sets `request.state.dry_run` to True/False based on header.
      - Reads are never gated — a GET with `X-Dry-Run: true` passes
        through with the flag set (some read endpoints may log or
        annotate differently; that's their choice).
      - On a MUTATING method with `X-Dry-Run: true`:
        * If the request path matches a registered entry in
          `DRY_RUN_SUPPORTED_ROUTES`, call_next is invoked and the
          route's `is_dry_run(request)` branch returns the preview.
        * If no entry matches, return **501 Not Implemented** with the
          `dry_run_unsupported` error envelope. No `call_next` — the
          request never reaches the handler, so no side effect is
          possible.
      - On a mutating method with a falsy / absent header, call_next is
        invoked normally — dry-run opt-in is optional for routes.

    This is the Phase 2 deny-by-default safety gate. Before this, a
    client sending `X-Dry-Run: true` to a non-opted-in route would
    silently execute the real mutation (because no route branched on
    the flag). That was the agent-safety lie Phase 2 had to close.
    """
    value = request.headers.get("X-Dry-Run")
    dry_run = _parse_dry_run_header(value)
    request.state.dry_run = dry_run

    if dry_run and request.method in _MUTATING_METHODS:
        path = request.url.path
        matched = any(
            request.method == method and regex.match(path)
            for method, regex in _SUPPORTED_MATCHER
        )
        if not matched:
            detail = (
                f"Route {request.method} {path} does not support dry-run. "
                "Retry without the X-Dry-Run header, or upgrade the ODIN "
                "backend to a version where this route opts in."
            )
            log.warning(
                "dry_run_unsupported method=%s path=%s supported_count=%d",
                request.method, path, len(_SUPPORTED_MATCHER),
            )
            return JSONResponse(
                status_code=501,
                content={
                    "detail": "Route does not support dry-run",
                    "error": {
                        "code": "dry_run_unsupported",
                        "detail": detail,
                        "retriable": False,
                    },
                },
            )

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
DRY_RUN_SUPPORTED_ROUTES: tuple[tuple[str, str], ...] = (
    # Phase 2 retrofit — populated in lockstep with route opt-ins.
    # Each entry has a paired `is_dry_run(request)` branch that returns
    # `dry_run_preview(...)` before any MQTT / DB / filesystem side effect.
    ("POST", "/api/v1/printers/{printer_id}/pause"),
    ("POST", "/api/v1/printers/{printer_id}/resume"),
    ("PATCH", "/api/v1/alerts/{alert_id}/read"),
    ("PATCH", "/api/v1/alerts/{alert_id}/dismiss"),
)


# Compiled once at module import. The middleware hot-path iterates this.
# Kept module-private so tests that override DRY_RUN_SUPPORTED_ROUTES
# via monkeypatch won't see stale regexes — tests that need to change
# the supported set should patch _SUPPORTED_MATCHER too (see
# test_dry_run_deny_by_default.py).
_SUPPORTED_MATCHER: list[tuple[str, Pattern[str]]] = _build_supported_matcher()
