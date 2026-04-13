"""
Contract test — log_audit must share the mutation transaction.

Guards R7 from the 2026-04-12 Codex adversarial review:
    log_audit() used to open its own commit boundary. Callers committed
    business changes first, then called log_audit() which internally did
    ANOTHER commit. If the audit insert failed (lock contention, constraint
    violation), the client saw a 500 and retried — but the original side
    effect had already persisted. Wrong error model; produced duplicate
    operations whose origin was invisible because the audit trail was
    exactly the part that failed.

This test is a source-level gate that guarantees:
  1. log_audit() does NOT call db.commit() internally
  2. Every caller of log_audit() has a db.commit() in the same handler
     (or calls another function that does — we can't inspect that perfectly
     statically, so we rely on a stronger check: the commit must appear
     lexically after log_audit within the enclosing function)

Run without container: pytest tests/test_contracts/test_audit_atomicity.py -v
"""

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
DEPENDENCIES = BACKEND_DIR / "core" / "dependencies.py"


def _enumerate_log_audit_call_sites() -> list[tuple[Path, int, str]]:
    """Return [(file, line_no, function_name)] for every log_audit() call in backend/."""
    sites = []
    for py in BACKEND_DIR.rglob("*.py"):
        # Skip the definition itself
        if py == DEPENDENCIES:
            continue
        try:
            source = py.read_text()
        except Exception:
            continue
        if "log_audit(" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        # Walk: find every Call whose .func.id == 'log_audit', and record enclosing function
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call):
                        fn = inner.func
                        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                        if name == "log_audit":
                            sites.append((py, inner.lineno, node.name))
    return sites


class TestLogAuditDoesNotCommitInternally:
    """log_audit itself must not commit — caller owns the transaction."""

    def test_log_audit_function_body_has_no_commit(self):
        source = DEPENDENCIES.read_text()
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "log_audit":
                target = node
                break
        assert target is not None, "log_audit is missing from core.dependencies"

        # Walk the body, check no db.commit() call
        for inner in ast.walk(target):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if isinstance(fn, ast.Attribute) and fn.attr == "commit":
                    if isinstance(fn.value, ast.Name) and fn.value.id == "db":
                        ast_src = ast.get_source_segment(source, inner) or ""
                        raise AssertionError(
                            f"log_audit() calls db.commit() internally. "
                            f"This breaks the R7 invariant: audit and business "
                            f"mutation must share a transaction. Remove the "
                            f"internal commit — the caller's commit covers both. "
                            f"Offending call: {ast_src!r}"
                        )

    def test_log_audit_docstring_tells_callers_to_commit(self):
        """The docstring is load-bearing — if someone removes it, future
        callers lose the invariant."""
        source = DEPENDENCIES.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "log_audit":
                docstring = ast.get_docstring(node) or ""
                assert "commit" in docstring.lower(), (
                    "log_audit's docstring must tell callers they own the commit. "
                    "Without this guidance, a future refactor can re-introduce "
                    "the commit-inside-log_audit bug."
                )
                return
        raise AssertionError("log_audit function not found")


class TestEveryCallSiteCommitsAfter:
    """Every log_audit() call must be followed by db.commit() in the same function."""

    def test_every_call_site_has_subsequent_commit(self):
        sites = _enumerate_log_audit_call_sites()
        assert len(sites) >= 40, (
            f"Found only {len(sites)} log_audit call sites in backend/. "
            f"Expected ~50+. Something is filtering calls out — check the "
            f"AST walk, or the codebase shrank unexpectedly."
        )

        missing = []
        for py, line_no, fn_name in sites:
            source = py.read_text()
            tree = ast.parse(source)

            # Find the containing function node
            fn_node = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == fn_name and node.lineno <= line_no <= (node.end_lineno or node.lineno):
                        fn_node = node
                        break
            if fn_node is None:
                continue  # couldn't resolve — skip, will be caught by other checks

            # Look for a db.commit() call AFTER line_no inside this function
            has_commit = False
            for inner in ast.walk(fn_node):
                if isinstance(inner, ast.Call) and inner.lineno > line_no:
                    fn_call = inner.func
                    if isinstance(fn_call, ast.Attribute) and fn_call.attr == "commit":
                        if isinstance(fn_call.value, ast.Name) and fn_call.value.id == "db":
                            has_commit = True
                            break

            if not has_commit:
                missing.append(f"{py.relative_to(BACKEND_DIR.parent)}:{line_no} in {fn_name}()")

        assert not missing, (
            "Found log_audit() call sites with no db.commit() afterwards in "
            "the same function. R7 requires audit staged + caller commits "
            "atomically. Offenders:\n"
            + "\n".join(f"  {m}" for m in missing)
        )
