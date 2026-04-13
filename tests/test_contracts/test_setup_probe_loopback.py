"""
Contract test — /setup/test-printer must enforce loopback-or-setup-token.

Guards R4 from the 2026-04-12 Codex adversarial review:
    routes_setup.py exposed an unauthenticated printer-probing endpoint
    that intentionally allowed connections to RFC1918 LAN addresses and
    actively performed HTTP+UDP probes to user-supplied hosts. If a fresh
    instance was internet-reachable (which is the default during setup),
    anyone could use /setup/test-printer as an internal network scanner
    against the host's local network — before any admin account existed.

Codex pass 2 (2026-04-13): the loopback-only fix broke documented
reverse-proxy deployments. The current gate accepts EITHER:
  1. Loopback request with no proxy headers, OR
  2. Matching X-ODIN-Setup-Token header against a host-filesystem
     token file.

Run without container: pytest tests/test_contracts/test_setup_probe_loopback.py -v
"""

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


class TestSetupProbeAccessGate:
    """/setup/test-printer must call the validate-access gate."""

    def test_handler_calls_validate_setup_access(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        assert fn_src, "setup_test_printer handler is missing"
        assert "_validate_setup_access" in fn_src, (
            "setup_test_printer must call _validate_setup_access(http_request) "
            "to enforce R4. Inlining the check is fine but please keep this "
            "test honest by aliasing it to the helper name."
        )

    def test_handler_keeps_setup_locked_check(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_test_printer")
        assert "_setup_is_locked" in fn_src, (
            "setup_test_printer no longer checks _setup_is_locked. "
            "Once setup is complete the endpoint must return 403; "
            "otherwise an attacker who can reach the box after install "
            "can re-enable wizard probes."
        )


class TestValidateSetupAccessGate:
    """_validate_setup_access — setup-token-only. No transport bypass.

    Codex pass 3 (2026-04-13): the loopback-no-proxy bypass was removed.
    Some reverse proxies don't set X-Forwarded-* headers, so loopback +
    no-proxy-header was indistinguishable from "internet caller arrived
    via proxy", which made the bypass unsafe. The gate is token-only now.
    """

    def test_helper_exists(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "_validate_setup_access")
        assert fn_src, (
            "_validate_setup_access helper is missing. R4 requires it as "
            "the single point that gates the printer probe."
        )

    def test_helper_does_not_have_loopback_bypass(self):
        """No transport-level bypass — token is universally required."""
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "_validate_setup_access")
        code_only = re.sub(r'""".*?"""', "", fn_src, flags=re.DOTALL)
        code_only = re.sub(r"#[^\n]*", "", code_only)
        # Loopback IP literals must NOT appear in the live code path.
        assert "127.0.0.1" not in code_only, (
            "Loopback bypass detected — codex pass 3 rejected this. "
            "Some proxies don't set X-Forwarded-*; loopback + no-XFF is "
            "indistinguishable from external caller. Token-only please."
        )
        assert "::1" not in code_only, (
            "IPv6 loopback bypass detected — same reasoning as above."
        )
        # And the gate must NOT consult client.host for an accept decision.
        assert "client.host" not in code_only and "client_host" not in code_only, (
            "Gate consults client.host. The token is the trust boundary; "
            "transport-level checks are bypassable."
        )

    def test_helper_does_not_trust_xforwarded_for(self):
        """X-Forwarded-For must NEVER be parsed as a trust signal in this gate."""
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "_validate_setup_access")
        code_only = re.sub(r'""".*?"""', "", fn_src, flags=re.DOTALL)
        code_only = re.sub(r"#[^\n]*", "", code_only)
        near = re.findall(r"forwarded[^\n]{0,80}\.split", code_only, re.IGNORECASE)
        assert not near, (
            "Gate appears to .split() a Forwarded/X-Forwarded-For "
            "header value. That makes the gate decision depend on an "
            "attacker-controlled header."
        )

    def test_helper_uses_constant_time_token_compare(self):
        """Setup-token compare must use compare_digest, not == ."""
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "_validate_setup_access")
        assert "compare_digest" in fn_src, (
            "Setup token comparison must use secrets.compare_digest to avoid "
            "timing-oracle leaks."
        )

    def test_helper_raises_403_on_failure(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "_validate_setup_access")
        assert "status_code=403" in fn_src, (
            "_validate_setup_access must raise HTTP 403 on failure. Returning "
            "without raising would let the printer probe execute."
        )


class TestSetupTokenLifecycle:
    """Setup token lifecycle — DB-backed, atomic, race-safe."""

    def test_ensure_setup_token_exists(self):
        source = ROUTES_SETUP.read_text()
        assert _get_function_source(source, "_ensure_setup_token"), (
            "_ensure_setup_token helper is missing — without it the operator "
            "behind a reverse proxy cannot complete setup."
        )

    def test_consume_setup_token_exists(self):
        source = ROUTES_SETUP.read_text()
        assert _get_function_source(source, "_consume_setup_token"), (
            "_consume_setup_token helper is missing — the token would persist "
            "after setup completes, leaving a long-lived secret on disk."
        )

    def test_setup_complete_consumes_token(self):
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_mark_complete")
        assert "_consume_setup_token" in fn_src, (
            "setup_mark_complete does not call _consume_setup_token. The "
            "token would survive setup and remain a credential."
        )

    def test_setup_status_ensures_token(self):
        """First /setup/status call when unlocked must mint the token so the
        operator sees it in logs without hunting around the filesystem."""
        source = ROUTES_SETUP.read_text()
        fn_src = _get_function_source(source, "setup_status")
        assert "_ensure_setup_token" in fn_src, (
            "setup_status does not call _ensure_setup_token when unlocked. "
            "The operator needs the token logged at that point."
        )

    def test_token_is_db_backed_not_filesystem(self):
        """Codex pass 3 round 2 (2026-04-13): per-replica filesystem state
        broke multi-replica Postgres (each container has its own .setup_token
        file) and multi-worker Uvicorn (race on file existence). Token must
        live in system_config so all replicas/workers share one truth."""
        source = ROUTES_SETUP.read_text()
        ensure_src = _get_function_source(source, "_ensure_setup_token")
        consume_src = _get_function_source(source, "_consume_setup_token")
        # Must touch system_config table.
        assert "system_config" in ensure_src, (
            "_ensure_setup_token must read/write the system_config table. "
            "Filesystem-only token doesn't survive multi-replica deployment."
        )
        assert "system_config" in consume_src, (
            "_consume_setup_token must DELETE from system_config."
        )
        # Must NOT use Path / cwd / write_text — those are the per-replica
        # patterns we're guarding against.
        for hostile in ("Path.cwd()", "write_text(", ".unlink()", "_setup_token_path"):
            assert hostile not in ensure_src, (
                f"_ensure_setup_token still uses filesystem primitive "
                f"{hostile!r}. Move to system_config storage."
            )

    def test_token_generation_is_race_safe(self):
        """Two workers calling _ensure_setup_token simultaneously must not
        both log new tokens and overwrite each other. Atomic INSERT (via
        the SystemConfig ORM model) with conflict-recovery is the contract."""
        source = ROUTES_SETUP.read_text()
        ensure_src = _get_function_source(source, "_ensure_setup_token")
        # Must INSERT a SystemConfig row (atomic single-winner via PK
        # constraint). ORM is required so the JSON column gets encoded
        # correctly on Postgres — raw text() INSERT crashes there.
        assert "SystemConfig(" in ensure_src and "db.add(" in ensure_src, (
            "_ensure_setup_token must INSERT through the SystemConfig ORM "
            "model (db.add(SystemConfig(...))). Raw text() INSERT bypasses "
            "JSON column encoding and crashes on Postgres."
        )
        # Must catch the conflict and re-read, not blindly overwrite.
        assert "rollback()" in ensure_src, (
            "_ensure_setup_token must rollback on insert conflict so the "
            "loser of the race re-reads the winner's token instead of "
            "logging a fresh one that doesn't match what the gate expects."
        )
        # Must NOT use raw INSERT INTO system_config (regression catcher).
        assert "INSERT INTO system_config" not in ensure_src, (
            "_ensure_setup_token regressed to raw text() INSERT. That "
            "bypasses SQLAlchemy JSON coercion and silently fails on "
            "Postgres ('invalid input syntax for type json'). Use the "
            "SystemConfig ORM model instead."
        )
