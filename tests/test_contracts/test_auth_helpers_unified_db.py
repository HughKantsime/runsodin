"""
Contract test — auth_helpers must use the app's main DB, not a separate path.

Guards R9 from the 2026-04-12 Codex adversarial review:
    auth_helpers.py opened its own sqlite3 connection via `DATABASE_PATH`
    env var. In non-container deployments, or when DATABASE_URL pointed
    at Postgres or a non-default sqlite path, the login_attempts table
    was stored in a different database than the app's real data. Result:
    either "all logins blocked because the helper DB is unavailable"
    (fail-closed on missing file) or "throttling silently tracks a
    different database than the app is using."

Fix: every helper takes `db: Session` from the caller's FastAPI
dependency and uses `db.execute(text(...))` on the shared session.
DATABASE_PATH env var is gone.

Run without container: pytest tests/test_contracts/test_auth_helpers_unified_db.py -v
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
AUTH_HELPERS = BACKEND_DIR / "core" / "auth_helpers.py"
ROUTES_AUTH = BACKEND_DIR / "modules" / "organizations" / "routes_auth.py"


def _get_function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    return ""


class TestAuthHelpersUnified:
    """Every helper must take Session and use it."""

    def test_no_more_database_path_env_var(self):
        """DATABASE_PATH must not be referenced in CODE (docstrings/comments
        explaining the historical pattern are fine and expected)."""
        source = AUTH_HELPERS.read_text()
        import re
        # Strip triple-quoted docstrings and line comments
        code_only = re.sub(r'"""[\s\S]*?"""', "", source)
        code_only = re.sub(r"'''[\s\S]*?'''", "", code_only)
        code_only = re.sub(r"#[^\n]*", "", code_only)
        assert "DATABASE_PATH" not in code_only, (
            "auth_helpers.py still references DATABASE_PATH in code. "
            "The R9 fix requires removing the split DB path so login "
            "throttling uses the same connection as the rest of the app."
        )

    def test_no_direct_sqlite3_import(self):
        source = AUTH_HELPERS.read_text()
        assert "import sqlite3" not in source, (
            "auth_helpers.py imports sqlite3 directly. The R9 fix routes "
            "all DB access through the app's SQLAlchemy session so "
            "DATABASE_URL (Postgres or non-default paths) is honored."
        )

    def test_check_rate_limit_takes_db(self):
        source = AUTH_HELPERS.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_check_rate_limit":
                args = [a.arg for a in node.args.args]
                assert args and args[0] == "db", (
                    f"_check_rate_limit signature is {args}. It must take "
                    f"'db' as its first argument so callers pass the shared "
                    f"Session instead of opening a new DB connection."
                )
                return
        raise AssertionError("_check_rate_limit not found")

    def test_record_login_attempt_takes_db_first(self):
        source = AUTH_HELPERS.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_record_login_attempt":
                args = [a.arg for a in node.args.args]
                assert args and args[0] == "db", (
                    f"_record_login_attempt signature is {args}. db must be "
                    f"first so the function signature matches the rest of the "
                    f"helpers and callers can pass it by position."
                )
                return
        raise AssertionError("_record_login_attempt not found")

    def test_is_locked_out_takes_db(self):
        source = AUTH_HELPERS.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_is_locked_out":
                args = [a.arg for a in node.args.args]
                assert args and args[0] == "db", (
                    f"_is_locked_out signature is {args}. First arg must be db."
                )
                return
        raise AssertionError("_is_locked_out not found")


class TestLoginRouteUsesUnifiedHelpers:
    """The login handler must pass its session to the helpers."""

    def test_calls_check_rate_limit_with_db(self):
        source = ROUTES_AUTH.read_text()
        fn_src = _get_function_source(source, "login")
        assert "_check_rate_limit(db, " in fn_src, (
            "login() does not pass db to _check_rate_limit. If we regress "
            "to the 1-arg form, a future refactor will re-introduce the "
            "separate-sqlite-connection pattern."
        )

    def test_calls_is_locked_out_with_db(self):
        source = ROUTES_AUTH.read_text()
        fn_src = _get_function_source(source, "login")
        assert "_is_locked_out(db, " in fn_src, (
            "login() does not pass db to _is_locked_out."
        )

    def test_calls_record_login_attempt_with_db_first(self):
        source = ROUTES_AUTH.read_text()
        fn_src = _get_function_source(source, "login")
        # db is first positional arg now
        assert "_record_login_attempt(db, " in fn_src, (
            "login() does not pass db to _record_login_attempt as the "
            "first positional argument. Check the call sites — the arg "
            "order changed."
        )


class TestFailClosedBehavior:
    """Fail-closed is preserved — an unreachable DB should block login."""

    def test_check_rate_limit_returns_true_on_exception(self):
        source = AUTH_HELPERS.read_text()
        fn_src = _get_function_source(source, "_check_rate_limit")
        assert "return True" in fn_src, (
            "_check_rate_limit no longer returns True on exception. "
            "Fail-closed (return True → block login) is load-bearing — a "
            "DB outage must not become a brute-force window."
        )
        # The return True should come after an except block
        except_idx = fn_src.find("except")
        return_true_idx = fn_src.find("return True")
        assert except_idx >= 0 and return_true_idx > except_idx, (
            "_check_rate_limit's return True is not inside the exception "
            "handler. Verify fail-closed still works on DB errors."
        )

    def test_is_locked_out_returns_true_on_exception(self):
        source = AUTH_HELPERS.read_text()
        fn_src = _get_function_source(source, "_is_locked_out")
        except_idx = fn_src.find("except")
        return_true_idx = fn_src.find("return True")
        assert except_idx >= 0 and return_true_idx > except_idx, (
            "_is_locked_out does not fail-closed on DB errors."
        )
