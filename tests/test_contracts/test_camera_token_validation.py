"""
Contract test — Camera route query-param tokens must go through full validation.

Guards R3 from the 2026-04-12 Codex adversarial review:
    camera_routes.py accepted a ?token= query param, called decode_token(),
    and trusted `sub` to load the user directly. This bypassed blacklist
    checks and accepted ws-only / mfa_pending / mfa_setup_required tokens
    that should not grant timelapse access.

This test verifies at the source level that the fix is in place and cannot
regress via a future copy-paste of the old pattern.

Run without container: pytest tests/test_contracts/test_camera_token_validation.py -v
"""

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
CAMERA_ROUTES = BACKEND_DIR / "modules" / "printers" / "camera_routes.py"
DEPENDENCIES = BACKEND_DIR / "core" / "dependencies.py"


class TestCameraRouteTokenValidation:
    """camera_routes.py must not short-circuit JWT validation for query-param tokens."""

    def test_camera_routes_does_not_use_decode_token_directly(self):
        """The raw decode_token shortcut from core.auth was the R3 vulnerability.

        If this import returns, someone added it back. Use validate_access_token
        from core.dependencies instead — it enforces blacklist + purpose checks.
        """
        source = CAMERA_ROUTES.read_text()
        assert "from core.auth import decode_token" not in source, (
            "camera_routes.py re-imported decode_token. Use "
            "core.dependencies.validate_access_token instead so the ?token= "
            "query param goes through blacklist and purpose-claim checks."
        )
        # Also catch the bare function call in case it was imported via *
        assert "decode_token(" not in source, (
            "camera_routes.py calls decode_token() directly. This bypasses the "
            "blacklist + purpose-claim checks that a full access token requires. "
            "Use validate_access_token(token, db) from core.dependencies."
        )

    def test_camera_routes_uses_validate_access_token(self):
        """The fix relies on validate_access_token being wired in."""
        source = CAMERA_ROUTES.read_text()
        assert "validate_access_token" in source, (
            "camera_routes.py does not reference validate_access_token. "
            "The R3 fix requires both /timelapses/{id}/video and "
            "/timelapses/{id}/stream to route ?token= through it."
        )

    def test_validate_access_token_checks_blacklist_and_purposes(self):
        """The helper itself must enforce the checks the camera routes delegate to."""
        source = DEPENDENCIES.read_text()

        # Extract the validate_access_token function body
        import ast
        tree = ast.parse(source)
        fn_src = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "validate_access_token":
                fn_src = ast.get_source_segment(source, node)
                break
        assert fn_src is not None, "validate_access_token function is missing from core.dependencies"

        # Must reject ws, mfa_pending, mfa_setup_required tokens
        assert '"ws"' in fn_src or "'ws'" in fn_src, (
            "validate_access_token does not check the ws purpose claim. "
            "WebSocket-scoped tokens must not grant full REST access."
        )
        assert "mfa_pending" in fn_src, (
            "validate_access_token does not reject mfa_pending tokens. "
            "A partially-authenticated user must not reach timelapse content."
        )
        assert "mfa_setup_required" in fn_src, (
            "validate_access_token does not reject mfa_setup_required tokens."
        )

        # Must check token_blacklist
        assert "token_blacklist" in fn_src, (
            "validate_access_token does not check token_blacklist. "
            "Logged-out sessions would remain valid until the JWT expires."
        )

    def test_camera_video_endpoints_still_require_auth(self):
        """The early-return 401 guard must remain — don't accidentally drop it."""
        source = CAMERA_ROUTES.read_text()
        # Both endpoints should still raise 401 when current_user is still None
        # after the token validation attempt.
        occurrences = source.count('raise HTTPException(status_code=401, detail="Not authenticated")')
        assert occurrences >= 2, (
            f"Expected at least 2 unauthenticated-401 raises in camera_routes.py "
            f"(one each for /video and /stream). Found {occurrences}. "
            f"If the guard was removed, timelapse content would be served to "
            f"anonymous requests with no token at all."
        )
