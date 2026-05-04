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
        return mod == "backend.modules" or mod.startswith("backend.modules.")
    if isinstance(node, ast.Import):
        return any(
            alias.name == "backend.modules" or alias.name.startswith("backend.modules.")
            for alias in node.names
        )
    return False


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
    type_checking_blocks: list[ast.If] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            type_checking_blocks.append(node)

    def _inside_type_checking(import_node: ast.AST) -> bool:
        for guard in type_checking_blocks:
            for body_node in ast.walk(guard):
                if body_node is import_node:
                    return True
        return False

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not _import_targets_backend_modules(node):
            continue
        if _inside_type_checking(node):
            continue
        line = source_lines[node.lineno - 1] if node.lineno - 1 < len(source_lines) else ""
        violations.append((node.lineno, line.strip()))

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
