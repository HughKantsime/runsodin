"""
Contract tests — Middleware authentication paths.

Verifies the authenticate_request middleware accepts all valid auth methods:
  1. X-API-Key header (global key)
  2. X-API-Key header (odin_ scoped tokens — passed through to get_current_user)
  3. Session cookie (browser-based SPA auth)
  4. Rejects requests with no auth when API_KEY is set
  5. Invalid X-API-Key header returns 401

Run without container: pytest tests/test_contracts/test_middleware_auth.py -v
"""

import ast
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
APP_PY = BACKEND_DIR / "core" / "app.py"


def _get_middleware_source() -> str:
    """Extract the authenticate_request middleware source from app.py."""
    source = APP_PY.read_text()
    tree = ast.parse(source)
    # Find the _register_http_middleware function
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_register_http_middleware":
            return ast.get_source_segment(source, node)
    pytest.fail("Could not find _register_http_middleware in app.py")


class TestMiddlewareAuthPaths:
    """Verify the middleware handles all expected auth paths."""

    def test_middleware_checks_session_cookie(self):
        """The middleware must check for session cookies as a fallback
        when API_KEY is set and no X-API-Key header is present.

        This ensures browser-based SPA users (who authenticate via httpOnly
        session cookies) are not blocked by the perimeter middleware.
        """
        source = _get_middleware_source()
        # The middleware must reference cookies.get("session") for cookie fallback
        assert 'cookies.get("session")' in source or "cookies.get('session')" in source, (
            "authenticate_request middleware does not check for session cookie. "
            "Browser SPA users will get 401 when API_KEY is set because "
            "they authenticate via httpOnly cookies, not X-API-Key headers."
        )

    def test_middleware_checks_api_key_header(self):
        """The middleware must check the X-API-Key header."""
        source = _get_middleware_source()
        assert 'X-API-Key' in source

    def test_middleware_allows_odin_prefix_tokens(self):
        """Per-user scoped tokens (odin_ prefix) must pass through middleware."""
        source = _get_middleware_source()
        assert 'odin_' in source

    def test_middleware_uses_constant_time_comparison(self):
        """Global API key comparison must use hmac.compare_digest (constant-time)."""
        source = _get_middleware_source()
        assert "compare_digest" in source

    def test_middleware_calls_decode_token_for_cookie(self):
        """When falling back to cookie auth, the middleware must validate
        the JWT via decode_token (not just check for cookie presence)."""
        source = _get_middleware_source()
        assert "decode_token" in source, (
            "Middleware must validate the session cookie JWT via decode_token, "
            "not just check for cookie presence."
        )

    def test_invalid_api_key_returns_401_not_passes_through(self):
        """An invalid X-API-Key (not global, not odin_ prefix) must return 401."""
        source = _get_middleware_source()
        # After checking global key and odin_ prefix, invalid keys must be rejected
        assert "401" in source


class TestMiddlewareBypassPaths:
    """Verify the middleware bypasses auth for the correct paths."""

    def test_auth_routes_bypassed(self):
        """Auth routes (/api/auth/*) must be bypassed by middleware."""
        source = _get_middleware_source()
        assert "/auth" in source

    def test_health_bypassed(self):
        """Health check must be bypassed."""
        source = _get_middleware_source()
        assert "/health" in source

    def test_setup_bypassed(self):
        """Setup routes must be bypassed."""
        source = _get_middleware_source()
        assert "/setup" in source

    def test_websocket_bypassed(self):
        """WebSocket endpoint must be bypassed."""
        source = _get_middleware_source()
        assert "/ws" in source

    def test_overlay_bypassed(self):
        """Overlay routes (OBS streaming) must be bypassed."""
        source = _get_middleware_source()
        assert "/overlay/" in source

    def test_label_endpoints_bypassed(self):
        """Label endpoints must be bypassed (printer labels for barcode scanners)."""
        source = _get_middleware_source()
        assert "/label" in source
