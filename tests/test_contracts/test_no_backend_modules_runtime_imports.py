"""
Contract test — Single sys.path root for `modules.*`.

Guards the regression that produced the ODIN_TELEMETRY_V2 silent-drop bug
(see docs/issues/ODIN-V2-IMPORT-PATH-BUG.md): the same Python source
reachable under two sys.path roots produces two distinct class objects,
which silently breaks isinstance() checks. The fix (commit 18054a2)
canonicalized 85 sites to `modules.*`. This test prevents regression.

Rule: NO runtime `from backend.modules` or `import backend.modules`
imports anywhere in the tree. Allowed ONLY inside `if TYPE_CHECKING:`
blocks (which never execute at runtime).

Why AST not regex: import statements inside `if TYPE_CHECKING:` blocks
are runtime-safe; a string scan can't tell them apart from runtime
imports. AST walking checks ancestor blocks correctly.

Run: pytest tests/test_contracts/test_no_backend_modules_runtime_imports.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Exemptions — files that legitimately need `from backend.modules.*`
# imports because of their deployment layout, AND that cannot share a
# Python process with the main app (so they cannot trigger the dual-root
# class-identity bug this contract guards against).
#
# Each entry must be paired with a one-line reason; if a future change
# can't articulate why a new file belongs here, that's the signal to
# canonicalize it instead.
# ---------------------------------------------------------------------------

EXEMPT_PATHS: dict[str, str] = {
    # Demo publisher runs in its own compose service / k8s Deployment with
    # the script mounted at /opt/odin-demo/ and backend at /app/backend.
    # Bare `from modules.*` is not resolvable in that layout; PYTHONPATH=/app
    # makes only `from backend.modules.*` reachable. Process-isolated from
    # the app, so the dual-root bug cannot occur. See top of the file.
    "ops/demo/demo_publisher.py": "process-isolated demo publisher; deployed layout requires backend.* prefix",
}


def _is_exempt(py_file: Path) -> bool:
    rel = py_file.relative_to(REPO_ROOT).as_posix()
    return rel in EXEMPT_PATHS


def _is_type_checking_guard(node: ast.If) -> bool:
    """True if `node` is `if TYPE_CHECKING:` (the only allowed guard)."""
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _import_targets_backend_modules(node: ast.AST) -> bool:
    """True if this import statement references `backend.modules.*`."""
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod == "backend.modules" or mod.startswith("backend.modules."):
            return True
        # `from backend import modules` — imports the same forbidden package
        # under a different alias path.
        if mod == "backend" and any(alias.name == "modules" for alias in node.names):
            return True
        return False
    if isinstance(node, ast.Import):
        return any(
            alias.name == "backend.modules" or alias.name.startswith("backend.modules.")
            for alias in node.names
        )
    return False


def _is_importlib_call_targeting_backend_modules(node: ast.AST) -> bool:
    """True if this is `importlib.import_module("backend.modules...")` with
    a literal string arg. Dynamic / computed module names are out of scope —
    this only catches the easy regression vector where someone hand-writes
    `import_module("backend.modules.X")` to dodge a static check."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    is_import_module = (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
    ) or (
        isinstance(func, ast.Name)
        and func.id == "import_module"
    )
    if not is_import_module:
        return False
    if not node.args:
        return False
    first_arg = node.args[0]
    if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
        return False
    return first_arg.value == "backend.modules" or first_arg.value.startswith("backend.modules.")


def _find_violations(py_file: Path) -> list[tuple[int, str]]:
    """Return [(lineno, source_line)] for every runtime backend.modules import."""
    try:
        source = py_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError:
        return []

    source_lines = source.splitlines()

    # Build an id-set of nodes that live inside `if TYPE_CHECKING:` block
    # bodies ONLY (not the orelse / else branch — that runs at runtime).
    type_checked_node_ids: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and _is_type_checking_guard(node)):
            continue
        for body_stmt in node.body:
            for descendant in ast.walk(body_stmt):
                type_checked_node_ids.add(id(descendant))

    def _emit(node: ast.AST, lineno: int) -> None:
        line = source_lines[lineno - 1] if lineno - 1 < len(source_lines) else ""
        violations.append((lineno, line.strip()))

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in type_checked_node_ids:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if _import_targets_backend_modules(node):
                _emit(node, node.lineno)
        elif _is_importlib_call_targeting_backend_modules(node):
            _emit(node, node.lineno)

    return violations


def _scanned_files() -> list[Path]:
    """All .py files in the repo, excluding venvs / build artefacts."""
    excluded_exact = {
        "__pycache__", ".git", "venv", "node_modules",
        "build", "dist", ".pytest_cache", ".mypy_cache", "frontend",
        "site-packages",
    }
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        parts = path.parts
        if any(p in excluded_exact for p in parts):
            continue
        if any(p.startswith(".venv") or p.endswith(".egg-info") for p in parts):
            continue
        out.append(path)
    return out


class TestNoBackendModulesRuntimeImports:
    """Regression guard for the V2 telemetry silent-drop bug."""

    def test_no_runtime_backend_modules_imports(self):
        """No `from backend.modules` or `import backend.modules` outside TYPE_CHECKING."""
        all_violations: list[str] = []
        for py_file in _scanned_files():
            if _is_exempt(py_file):
                continue
            for lineno, line in _find_violations(py_file):
                rel = py_file.relative_to(REPO_ROOT)
                all_violations.append(f"{rel}:{lineno}: {line}")

        assert not all_violations, (
            f"Runtime `backend.modules` imports found ({len(all_violations)}). "
            "These create dual sys.path roots → distinct class objects → silent "
            "isinstance() failures. See docs/issues/ODIN-V2-IMPORT-PATH-BUG.md.\n"
            "Fix: switch to `from modules.*` (the canonical root). For type-only "
            "references, wrap in `if TYPE_CHECKING:`.\n\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in all_violations)
        )

    def test_scanner_finds_repo_files(self):
        """Sanity: the scanner walks at least 100 .py files."""
        files = _scanned_files()
        assert len(files) >= 100, (
            f"Scanner found only {len(files)} files — repo layout may have changed."
        )

    def test_type_checking_guard_is_recognized(self):
        """Sanity: a stub `if TYPE_CHECKING:` block exempts its imports."""
        stub = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from backend.modules.printers.telemetry.state import PrinterStatus\n"
        )
        tmp = REPO_ROOT / "tests" / "test_contracts" / "_tc_guard_fixture.py"
        try:
            tmp.write_text(stub, encoding="utf-8")
            assert _find_violations(tmp) == [], (
                "TYPE_CHECKING-guarded import was incorrectly flagged."
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_runtime_import_is_flagged(self):
        """Sanity: a top-level runtime import IS flagged."""
        stub = "from backend.modules.printers.telemetry.state import PrinterStatus\n"
        tmp = REPO_ROOT / "tests" / "test_contracts" / "_tc_runtime_fixture.py"
        try:
            tmp.write_text(stub, encoding="utf-8")
            violations = _find_violations(tmp)
            assert len(violations) == 1, (
                f"Runtime import not flagged. Got: {violations}"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_else_branch_of_type_checking_is_not_exempt(self):
        """The `else:` branch of `if TYPE_CHECKING:` runs at runtime, so any
        `backend.modules` import there must still be flagged."""
        stub = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    pass\n"
            "else:\n"
            "    from backend.modules.printers.telemetry.state import PrinterStatus\n"
        )
        tmp = REPO_ROOT / "tests" / "test_contracts" / "_tc_else_fixture.py"
        try:
            tmp.write_text(stub, encoding="utf-8")
            violations = _find_violations(tmp)
            assert len(violations) == 1, (
                f"Runtime import in else-branch was not flagged. Got: {violations}"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_from_backend_import_modules_is_flagged(self):
        """`from backend import modules` reaches the same forbidden package."""
        stub = "from backend import modules\n"
        tmp = REPO_ROOT / "tests" / "test_contracts" / "_tc_alias_fixture.py"
        try:
            tmp.write_text(stub, encoding="utf-8")
            violations = _find_violations(tmp)
            assert len(violations) == 1, (
                f"`from backend import modules` was not flagged. Got: {violations}"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_importlib_string_literal_is_flagged(self):
        """`importlib.import_module("backend.modules...")` with a literal
        string is flagged. Dynamic/computed names stay out of scope."""
        stub = (
            "import importlib\n"
            "importlib.import_module('backend.modules.printers.telemetry.state')\n"
        )
        tmp = REPO_ROOT / "tests" / "test_contracts" / "_tc_importlib_fixture.py"
        try:
            tmp.write_text(stub, encoding="utf-8")
            violations = _find_violations(tmp)
            assert len(violations) == 1, (
                f"importlib.import_module literal not flagged. Got: {violations}"
            )
        finally:
            tmp.unlink(missing_ok=True)

    def test_exempt_paths_exist(self):
        """Every exempt path must actually exist — stale exemptions hide
        regressions and creep is the slow death of contract tests."""
        for rel_path in EXEMPT_PATHS:
            assert (REPO_ROOT / rel_path).is_file(), (
                f"EXEMPT_PATHS references missing file: {rel_path!r}. "
                f"If the file moved, update the entry; if it was deleted, drop it."
            )

    def test_exempt_paths_have_reasons(self):
        """Each exemption must have a non-empty reason — silent exemptions
        decay into 'we always did it that way'."""
        for rel_path, reason in EXEMPT_PATHS.items():
            assert reason and reason.strip(), (
                f"EXEMPT_PATHS[{rel_path!r}] has no reason"
            )

    def test_importlib_dynamic_arg_is_not_flagged(self):
        """Computed module names (variables, f-strings) stay out of scope —
        this contract is intentionally narrow."""
        stub = (
            "import importlib\n"
            "name = 'backend.modules.printers'\n"
            "importlib.import_module(name)\n"
        )
        tmp = REPO_ROOT / "tests" / "test_contracts" / "_tc_importlib_dyn_fixture.py"
        try:
            tmp.write_text(stub, encoding="utf-8")
            violations = _find_violations(tmp)
            assert violations == [], (
                f"Dynamic importlib arg was incorrectly flagged. Got: {violations}"
            )
        finally:
            tmp.unlink(missing_ok=True)
