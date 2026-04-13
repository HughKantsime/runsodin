"""
Contract test — /setup/test-printer must be loopback-only.

Guards R4 from the 2026-04-12 Codex adversarial review:
    routes_setup.py exposed an unauthenticated printer-probing endpoint
    that intentionally allowed connections to RFC1918 LAN addresses and
    actively performed HTTP+UDP probes to user-supplied hosts. If a fresh
    instance was internet-reachable (which is the default during setup),
    anyone could use /setup/test-printer as an internal network scanner
    against the host's local network — before any admin account existed.

Fix: reject requests that don't come from loopback (127.0.0.1 / ::1 /
localhost). Setup is expected to run from the localhost web UI. Remote
setup requires a tunnel (ssh -L, tailscale), not bare network exposure.

Run without container: pytest tests/test_contracts/test_setup_probe_loopback.py -v
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
ROUTES_SETUP = BACKEND_DIR / "modules" / "system" / "routes_setup.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestSetupProbeLoopbackOnly:
    """/setup/test-printer must reject non-loopback requests."""

    def test_handler_accepts_http_request(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        assert fn_src, "setup_test_printer handler is missing"
        # Must accept the Request object so it can inspect client.host
        assert "Request" in fn_src, (
            "setup_test_printer does not receive a Request parameter. "
            "Without it, the handler cannot check client.host to enforce "
            "loopback-only access (R4)."
        )
        # More specifically, the http_request parameter + client.host usage:
        assert "http_request" in fn_src or "request.client" in fn_src, (
            "setup_test_printer does not inspect the HTTP client address. "
            "Need to read request.client.host to distinguish loopback from "
            "remote callers."
        )

    def test_handler_checks_loopback_addresses(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        # Must check for loopback IPs explicitly
        assert "127.0.0.1" in fn_src, (
            "setup_test_printer does not allow 127.0.0.1. Local setup UI "
            "must still work; we're not sealing the endpoint entirely."
        )
        assert "::1" in fn_src, (
            "setup_test_printer does not allow ::1 (IPv6 loopback). Some "
            "dual-stack macOS/Linux setups route localhost to ::1 first."
        )

    def test_handler_does_not_trust_xforwarded_for(self):
        """The loopback check must not be bypassable via X-Forwarded-For.

        We inspect only headers.get()/headers[] accesses — the string
        "X-Forwarded-For" may legitimately appear in comments explaining
        why we don't use it.
        """
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        # Strip comments and docstrings before looking for header access
        import re
        code_only = re.sub(r'""".*?"""', "", fn_src, flags=re.DOTALL)
        code_only = re.sub(r"#[^\n]*", "", code_only)
        forbidden_patterns = [
            'headers.get("x-forwarded-for"',
            "headers.get('x-forwarded-for'",
            'headers.get("X-Forwarded-For"',
            "headers.get('X-Forwarded-For'",
            'headers["x-forwarded-for"',
            'headers["X-Forwarded-For"',
        ]
        hits = [p for p in forbidden_patterns if p in code_only]
        assert not hits, (
            f"setup_test_printer reads X-Forwarded-For: {hits}. Don't — "
            f"it's attacker-controlled in the exposure scenario this check "
            f"is designed for (direct internet exposure, no real proxy in "
            f"front)."
        )

    def test_non_loopback_returns_403(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        # Must raise 403 for non-loopback, not silently succeed or fall through
        # Find the loopback check block
        loopback_idx = fn_src.find("127.0.0.1")
        # Check that within a reasonable window after, we raise 403
        after_check = fn_src[loopback_idx:loopback_idx + 1200]
        assert "status_code=403" in after_check, (
            "setup_test_printer does not raise 403 for non-loopback clients. "
            "Silent fall-through would still execute the outbound probe."
        )

    def test_setup_locked_check_still_present(self):
        """Don't remove the existing 'setup already completed' guard when
        adding the loopback check."""
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        assert "_setup_is_locked" in fn_src, (
            "setup_test_printer no longer checks _setup_is_locked. "
            "The endpoint must return 403 after setup has completed, "
            "otherwise you can enable setup again after wipe by hitting "
            "this endpoint from loopback."
        )
