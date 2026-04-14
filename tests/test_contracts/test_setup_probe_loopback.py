"""
Contract test — /setup/test-printer gated on first-user-wins invariant.

v1.8.8: this replaces the v1.8.6 token-gate tests. The setup-token
mechanism was deleted entirely; the new gate is the same invariant
WordPress / Ghost / Immich / Jellyfin / Portainer use — setup routes
are reachable until an admin is claimed, then they close.

The "freshly-installed ODIN on the internet is a LAN scanner" threat
that motivated the token gate is now addressed at install time:
install.sh probes public reachability and refuses without
`--force-public`. See DECISION-012 in conductor/decision-log.md.

Run: pytest tests/test_contracts/test_setup_probe_loopback.py -v
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
ROUTES_SETUP = BACKEND_DIR / "modules" / "system" / "routes_setup.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestFirstUserWinsGate:
    """The gate must call _validate_setup_phase and that helper must
    enforce: refuse if setup complete OR any user exists."""

    def test_handler_calls_validate_setup_phase(self):
        source = ROUTES_SETUP.read_text()
        fn = _get_function_source(source, "setup_test_printer")
        assert fn, "setup_test_printer handler is missing"
        assert "_validate_setup_phase" in fn, (
            "setup_test_printer must call _validate_setup_phase(db). "
            "This is the v1.8.8 first-user-wins gate that replaced "
            "the v1.8.6 token-based _validate_setup_access."
        )

    def test_gate_helper_exists(self):
        source = ROUTES_SETUP.read_text()
        fn = _get_function_source(source, "_validate_setup_phase")
        assert fn, (
            "_validate_setup_phase helper is missing. Without it the "
            "handler reference in setup_test_printer is a NameError."
        )

    def test_gate_blocks_when_setup_complete(self):
        source = ROUTES_SETUP.read_text()
        fn = _get_function_source(source, "_validate_setup_phase")
        assert "_setup_is_complete" in fn, (
            "_validate_setup_phase must consult _setup_is_complete. "
            "Once setup is explicitly marked complete, setup routes "
            "close regardless of user state."
        )
        assert "status_code=403" in fn, (
            "Gate must raise HTTP 403 on refusal."
        )

    def test_gate_blocks_when_users_exist(self):
        source = ROUTES_SETUP.read_text()
        fn = _get_function_source(source, "_validate_setup_phase")
        assert "_setup_users_exist" in fn, (
            "_validate_setup_phase must consult _setup_users_exist. "
            "First-user-wins means once any admin is claimed, setup "
            "routes close — even if setup_complete hasn't been marked."
        )


class TestTokenMachineryRetired:
    """Every trace of the v1.8.6 setup-token mechanism must be gone.

    Leaving dead references around a deleted subsystem is how dead code
    creeps back into production. These tests pin the removal."""

    def test_no_setup_token_symbols_in_routes_setup(self):
        source = ROUTES_SETUP.read_text()
        # Strip comments + docstrings so we catch live code references
        # only. We allow historical commentary in comments.
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        # Remove full-line comments only; preserve # in strings.
        code_only = "\n".join(
            line for line in code_only.splitlines()
            if not line.strip().startswith("#")
        )

        forbidden = [
            "_SETUP_TOKEN_HEADER",
            "_SETUP_TOKEN_DB_KEY",
            "_read_setup_token",
            "_ensure_setup_token",
            "_consume_setup_token",
            "_validate_setup_access",
            "X-ODIN-Setup-Token",
        ]
        hits = [s for s in forbidden if s in code_only]
        assert not hits, (
            f"Setup-token machinery not fully retired — still found: {hits}. "
            "These symbols must be gone from live code (comments are OK). "
            "The v1.8.8 onboarding refactor deletes the token entirely; "
            "leaving stale references invites future bugs."
        )

    def test_setup_mark_complete_no_longer_touches_token(self):
        source = ROUTES_SETUP.read_text()
        fn = _get_function_source(source, "setup_mark_complete")
        assert "_consume_setup_token" not in fn, (
            "setup_mark_complete still calls _consume_setup_token. "
            "The token is gone; the call is a NameError waiting to "
            "happen."
        )

    def test_setup_status_no_longer_mints_token(self):
        source = ROUTES_SETUP.read_text()
        fn = _get_function_source(source, "setup_status")
        assert "_ensure_setup_token" not in fn, (
            "setup_status still calls _ensure_setup_token. The token "
            "machinery is gone; this call is a NameError."
        )
