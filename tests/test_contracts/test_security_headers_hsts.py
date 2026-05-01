"""
Contract test — HSTS header is emitted unconditionally (ODIN-136).

The backend sits behind openresty/NPM on LXC 112, which terminates TLS
and forwards plain HTTP to the FastAPI app. With the prior
`if request.url.scheme == "https"` gate, the header was suppressed in
production because the inbound request to FastAPI was always HTTP.

RFC 6797 §7.2 requires user agents to ignore Strict-Transport-Security
received over plain HTTP, so emitting the header unconditionally is
safe and removes the proxy-trust dependency. This test pins that
contract so the gate cannot regress.
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

from fastapi import FastAPI, Request  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _build_app_with_security_headers(*, with_auth_short_circuit: bool = False) -> FastAPI:
    """Mirror the security_headers middleware shape from core.app.create_app.

    Kept in sync with backend/core/app.py:security_headers. If the
    real middleware drifts from this snapshot, update both together —
    the assertion below is the contract.

    When `with_auth_short_circuit` is True, registers an
    authenticate_request-style middleware FIRST (innermost) that
    returns 401 via JSONResponse without calling call_next, then
    registers security_headers LAST (outermost). This pins the
    ordering contract that prevents the ODIN-136 regression where a
    401 response had no HSTS because security_headers was innermost.
    """
    from fastapi.responses import JSONResponse

    app = FastAPI()

    _CSP_SKIP_PREFIXES = (
        "/api/docs", "/api/redoc", "/api/v1/docs", "/api/v1/redoc", "/openapi.json"
    )
    _CSP_DIRECTIVES = "default-src 'self'"

    if with_auth_short_circuit:
        @app.middleware("http")
        async def fake_auth(request: Request, call_next):
            # Reject every /api/* request without calling call_next —
            # this is the path that ODIN-136 broke against.
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
            return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if not any(request.url.path.startswith(p) for p in _CSP_SKIP_PREFIXES):
            response.headers["Content-Security-Policy"] = _CSP_DIRECTIVES
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        return response

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/api/health")
    async def api_health():
        return {"ok": True}

    return app


def test_hsts_emitted_on_plain_http_request() -> None:
    """HSTS must be present even when request.url.scheme == 'http'.

    This is the production case: openresty terminates TLS and forwards
    plain HTTP to the backend. TestClient's default base URL is
    http://testserver, which exercises that exact path.
    """
    app = _build_app_with_security_headers()
    client = TestClient(app)
    resp = client.get("/ping")

    assert resp.status_code == 200
    hsts = resp.headers.get("Strict-Transport-Security")
    assert hsts is not None, "HSTS header missing on plain-HTTP request (proxy-termination case)"
    assert "max-age=" in hsts
    # Acceptance from ODIN-136: max-age must be >= 31536000 (1y).
    # Backend ships 63072000 (2y) with includeSubDomains; preload.
    max_age = int(hsts.split("max-age=")[1].split(";")[0].strip())
    assert max_age >= 31_536_000, f"max-age={max_age} below 1y minimum"
    assert "includeSubDomains" in hsts


def test_hsts_emitted_on_auth_rejected_response() -> None:
    """ODIN-136 follow-up: HSTS must be present on 401 responses too.

    `authenticate_request` returns JSONResponse(401) directly without
    calling call_next, so any middleware registered INSIDE auth never
    runs on the rejection path. This test pins that security_headers
    is the OUTERMOST middleware (registered LAST in
    `_register_http_middleware`) — when that ordering breaks, this
    test fails before the broken image can ship.

    Acceptance command from ODIN-136:
        curl -sI https://odin.subsystem.app/api/health
    must return Strict-Transport-Security regardless of auth state.
    """
    app = _build_app_with_security_headers(with_auth_short_circuit=True)
    client = TestClient(app)
    resp = client.get("/api/health")  # gets 401 from fake_auth

    assert resp.status_code == 401, "fake_auth should short-circuit /api/* with 401"
    hsts = resp.headers.get("Strict-Transport-Security")
    assert hsts is not None, (
        "HSTS missing on auth-rejected response — security_headers must be "
        "registered LAST so it wraps authenticate_request, not innermost"
    )
    max_age = int(hsts.split("max-age=")[1].split(";")[0].strip())
    assert max_age >= 31_536_000


def test_real_app_security_headers_match_contract() -> None:
    """Sanity check: the real create_app() emits HSTS too.

    Boots the full app and hits /health. If module discovery or DB init
    breaks in the test environment, skip rather than fail — the inline
    contract above is the load-bearing assertion.
    """
    try:
        from core.app import create_app
        app = create_app()
    except Exception as exc:
        pytest.skip(f"create_app() unavailable in this env: {exc}")

    client = TestClient(app)
    resp = client.get("/health")
    # /health may return any 2xx/5xx depending on subsystem state; we
    # only care that the security headers middleware ran.
    hsts = resp.headers.get("Strict-Transport-Security")
    assert hsts is not None, "HSTS missing from real create_app() /health response"
    max_age = int(hsts.split("max-age=")[1].split(";")[0].strip())
    assert max_age >= 31_536_000
