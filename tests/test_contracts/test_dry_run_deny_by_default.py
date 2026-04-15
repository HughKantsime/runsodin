"""
Contract test — dry_run_middleware deny-by-default safety gate (Phase 2, R1).

Before this middleware change, `X-Dry-Run: true` on any mutating route
silently executed the real mutation unless the route itself branched on
`is_dry_run(request)`. Most routes don't — Phase 2 is a gradual retrofit.

The middleware now enforces **deny-by-default**: if a mutating method
(POST/PUT/PATCH/DELETE) receives `X-Dry-Run: true` AND the request path
does not match any entry in `DRY_RUN_SUPPORTED_ROUTES`, the middleware
returns 501 with the `dry_run_unsupported` error envelope — without
calling the route handler. Side effects are impossible.

This keeps the entire ODIN surface safe during the Phase 2 migration
window: a route that hasn't been retrofitted yet is automatically
protected, not silently broken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")
pytest.importorskip("starlette", reason="Starlette not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Top-level imports so FastAPI route signatures resolve cleanly. Nesting
# these inside the fixture function was causing FastAPI to misidentify
# `request: Request` as a query parameter (it couldn't match the Request
# class reference).
from fastapi import FastAPI, Request  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from core.middleware import dry_run as dry_run_mod  # noqa: E402
from core.middleware.dry_run import (  # noqa: E402
    _build_supported_matcher,
    dry_run_middleware,
    dry_run_preview,
    is_dry_run,
)


def _build_test_app(supported_routes: tuple[tuple[str, str], ...]):
    """Build a minimal FastAPI app with the dry_run_middleware applied and
    the supported-routes allowlist monkeypatched for the duration of the test."""
    # Swap in custom supported routes + rebuild matcher.
    dry_run_mod.DRY_RUN_SUPPORTED_ROUTES = supported_routes  # type: ignore[attr-defined]
    dry_run_mod._SUPPORTED_MATCHER = _build_supported_matcher()

    app = FastAPI()
    app.middleware("http")(dry_run_middleware)

    # A registered POST that checks the dry-run flag.
    @app.post("/api/v1/printers/{printer_id}/pause")
    async def pause_printer(printer_id: int, request: Request):
        if is_dry_run(request):
            return dry_run_preview(
                would_execute={"action": "pause", "printer_id": printer_id},
            )
        return {"success": True, "printer_id": printer_id}

    # An unregistered POST — must 501 on dry-run.
    @app.post("/api/v1/alerts/{alert_id}/read")
    async def mark_alert_read(alert_id: int, request: Request):
        # Deliberately does NOT check is_dry_run — it's not in the
        # allowlist, so the middleware should intercept before we get here.
        return {"success": True, "alert_id": alert_id, "side_effect": "mutated"}

    # An unregistered PATCH — same deal.
    @app.patch("/api/v1/spools/{spool_id}/use")
    async def consume_spool(spool_id: int):
        return {"success": True, "spool_id": spool_id, "side_effect": "mutated"}

    # A GET — never 501s, X-Dry-Run is a no-op.
    @app.get("/api/v1/printers")
    async def list_printers(request: Request):
        return {"printers": [], "dry_run_flag": is_dry_run(request)}

    return TestClient(app)


@pytest.fixture
def client():
    return _build_test_app(
        supported_routes=(("POST", "/api/v1/printers/{printer_id}/pause"),)
    )


# ---------------------------------------------------------------------------
# Core deny-by-default invariants
# ---------------------------------------------------------------------------


class TestDenyByDefault:
    def test_unregistered_post_with_dry_run_returns_501(self, client):
        r = client.post("/api/v1/alerts/5/read", headers={"X-Dry-Run": "true"})
        assert r.status_code == 501
        body = r.json()
        assert body["error"]["code"] == "dry_run_unsupported"
        assert body["error"]["retriable"] is False
        assert "detail" in body  # dual-shape: top-level + error.detail
        # Critical: route body never ran — no "mutated" side-effect marker.
        assert "side_effect" not in body

    def test_unregistered_patch_with_dry_run_returns_501(self, client):
        r = client.patch(
            "/api/v1/spools/7/use",
            headers={"X-Dry-Run": "true"},
            json={"grams": 10},
        )
        assert r.status_code == 501
        assert r.json()["error"]["code"] == "dry_run_unsupported"

    def test_registered_post_with_dry_run_passes_to_handler(self, client):
        r = client.post("/api/v1/printers/42/pause", headers={"X-Dry-Run": "true"})
        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is True
        assert body["would_execute"]["action"] == "pause"
        assert body["would_execute"]["printer_id"] == 42

    def test_unregistered_post_without_header_executes_normally(self, client):
        r = client.post("/api/v1/alerts/5/read")
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_unregistered_post_with_falsy_header_executes_normally(self, client):
        for value in ("false", "0", "no", "off", ""):
            r = client.post(
                "/api/v1/alerts/5/read",
                headers={"X-Dry-Run": value},
            )
            assert r.status_code == 200, f"falsy value {value!r} should pass through"

    def test_get_with_dry_run_never_501s(self, client):
        r = client.get("/api/v1/printers", headers={"X-Dry-Run": "true"})
        assert r.status_code == 200
        # Flag is still set on request state for the route to read if it wants.
        assert r.json()["dry_run_flag"] is True


# ---------------------------------------------------------------------------
# Path-match precision — the critical false-positive guard
# ---------------------------------------------------------------------------


class TestPathMatchPrecision:
    """The regex must not over-match. `/printers/{id}/pause` must not
    also match `/printers/{id}/logs` or `/printers/{id}/resume`."""

    def test_different_suffix_does_not_match(self):
        # Register /pause but try /logs on the same prefix.
        client = _build_test_app(
            supported_routes=(("POST", "/api/v1/printers/{printer_id}/pause"),)
        )

        from fastapi import Request

        @client.app.post("/api/v1/printers/{printer_id}/logs")
        async def printer_logs(printer_id: int):
            return {"logs": [], "side_effect": "mutated"}

        r = client.post("/api/v1/printers/42/logs", headers={"X-Dry-Run": "true"})
        assert r.status_code == 501, (
            "Path /printers/42/logs must NOT match /printers/{id}/pause regex — "
            "if it does, an unsupported route would leak through."
        )

    def test_different_method_does_not_match(self):
        # Register POST /pause. A PATCH /pause must not match.
        client = _build_test_app(
            supported_routes=(("POST", "/api/v1/printers/{printer_id}/pause"),)
        )

        @client.app.patch("/api/v1/printers/{printer_id}/pause")
        async def patch_pause(printer_id: int):
            return {"success": True, "side_effect": "mutated"}

        r = client.patch(
            "/api/v1/printers/42/pause", headers={"X-Dry-Run": "true"}
        )
        assert r.status_code == 501

    def test_multi_segment_in_single_param_slot_does_not_match(self):
        """A path template `{id}` matches exactly one segment, not multiple.
        `/printers/a/b/pause` must NOT match `/printers/{id}/pause`."""
        client = _build_test_app(
            supported_routes=(("POST", "/api/v1/printers/{printer_id}/pause"),)
        )

        # No route registered for this; middleware should 501 first, but
        # even absent a route, the response must be 501 (or 404 AFTER 501
        # would have returned — we want 501 to be the outcome).
        r = client.post("/api/v1/printers/a/b/pause", headers={"X-Dry-Run": "true"})
        # Middleware rejects before routing. 501 is required.
        assert r.status_code == 501


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    def test_error_envelope_matches_odin_contract(self, client):
        r = client.post("/api/v1/alerts/5/read", headers={"X-Dry-Run": "true"})
        body = r.json()
        # Dual-shape: legacy `detail` + agent `error.{code,detail,retriable}`.
        assert isinstance(body.get("detail"), str)
        assert isinstance(body.get("error"), dict)
        err = body["error"]
        assert err["code"] == "dry_run_unsupported"
        assert isinstance(err["detail"], str)
        assert err["retriable"] is False


# ---------------------------------------------------------------------------
# Header parsing variations
# ---------------------------------------------------------------------------


class TestHeaderParsing:
    def test_truthy_variants(self, client):
        for value in ("true", "True", "TRUE", "1", "yes", "on"):
            r = client.post(
                "/api/v1/alerts/5/read", headers={"X-Dry-Run": value}
            )
            assert r.status_code == 501, f"{value!r} should be parsed as truthy"

    def test_whitespace_is_stripped(self, client):
        r = client.post(
            "/api/v1/alerts/5/read", headers={"X-Dry-Run": "  true  "}
        )
        assert r.status_code == 501

    def test_unknown_values_are_falsy(self, client):
        for value in ("maybe", "perhaps", "2"):
            r = client.post(
                "/api/v1/alerts/5/read", headers={"X-Dry-Run": value}
            )
            assert r.status_code == 200, f"{value!r} should be parsed as falsy"
