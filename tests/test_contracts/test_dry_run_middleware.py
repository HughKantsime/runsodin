"""
Contract test — X-Dry-Run middleware + helper (v1.8.9).

Verifies:
  1. Header parsing: `true`/`1`/`yes`/`on` enable dry-run; others don't.
  2. Middleware sets `request.state.dry_run` to the parsed value.
  3. `is_dry_run()` helper reads the flag safely.
  4. `dry_run_preview()` builds the canonical response envelope.
  5. `DRY_RUN_SUPPORTED_ROUTES` enumeration has the expected shape.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_parse_dry_run_header_truthy_values():
    from core.middleware.dry_run import _parse_dry_run_header

    for value in ("true", "True", "TRUE", "1", "yes", "on", " true "):
        assert _parse_dry_run_header(value) is True, f"{value!r} should enable"


def test_parse_dry_run_header_falsy_values():
    from core.middleware.dry_run import _parse_dry_run_header

    for value in ("false", "0", "no", "off", "", None, "maybe", "dry-run"):
        assert _parse_dry_run_header(value) is False, f"{value!r} should disable"


def test_is_dry_run_returns_false_when_unset():
    from core.middleware.dry_run import is_dry_run

    request = SimpleNamespace(state=SimpleNamespace())
    assert is_dry_run(request) is False


def test_is_dry_run_returns_true_when_set():
    from core.middleware.dry_run import is_dry_run

    request = SimpleNamespace(state=SimpleNamespace(dry_run=True))
    assert is_dry_run(request) is True


def test_dry_run_preview_minimal():
    from core.middleware.dry_run import dry_run_preview

    out = dry_run_preview(would_execute={"action": "foo"})
    assert out == {"dry_run": True, "would_execute": {"action": "foo"}}


def test_dry_run_preview_with_next_actions_and_notes():
    from core.middleware.dry_run import dry_run_preview

    out = dry_run_preview(
        would_execute={"a": 1},
        next_actions=[{"tool": "list_queue"}],
        notes="No DB changes applied.",
    )
    assert out["dry_run"] is True
    assert out["would_execute"] == {"a": 1}
    assert out["next_actions"] == [{"tool": "list_queue"}]
    assert out["notes"] == "No DB changes applied."


def test_middleware_sets_flag_on_header():
    """The middleware should read the header and set request.state.dry_run."""
    import asyncio

    from core.middleware.dry_run import dry_run_middleware

    # Fake Request object with headers + state — enough for the
    # middleware to write to.
    class _Headers:
        def __init__(self, h):
            self._h = h
        def get(self, k, default=None):
            return self._h.get(k, default)

    class _Req:
        def __init__(self, headers):
            self.headers = _Headers(headers)
            self.state = SimpleNamespace()

    async def _call_next(request):
        return request  # echo for assertion

    req = _Req({"X-Dry-Run": "true"})
    result = asyncio.run(dry_run_middleware(req, _call_next))
    assert result.state.dry_run is True

    req2 = _Req({})
    result2 = asyncio.run(dry_run_middleware(req2, _call_next))
    assert result2.state.dry_run is False

    # Case-insensitive header value
    req3 = _Req({"X-Dry-Run": "TRUE"})
    result3 = asyncio.run(dry_run_middleware(req3, _call_next))
    assert result3.state.dry_run is True


def test_supported_routes_enumeration_shape():
    """The canonical opt-in list has (method, path) tuples.

    Phase 1 ships with an empty tuple — codex pass 1 (2026-04-14)
    flagged that listing routes here without a matching
    `is_dry_run(request)` branch in each handler is a dangerous
    contract bug: clients read the registry, assume preview is safe,
    but the real mutation still runs. The tuple is populated in
    Phase 2 one entry at a time, in lockstep with route retrofits.
    """
    from core.middleware.dry_run import DRY_RUN_SUPPORTED_ROUTES

    # Shape only; do not assert length.
    for entry in DRY_RUN_SUPPORTED_ROUTES:
        assert isinstance(entry, tuple) and len(entry) == 2
        method, path = entry
        assert method in {"POST", "PUT", "PATCH", "DELETE"}
        assert path.startswith("/api/"), f"Path must be api-scoped: {path}"
