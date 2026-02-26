"""
Contract tests — Architecture lint.

Automated guardrails for the modular architecture established in v1.4.0–v1.4.1.
These tests enforce structural conventions that prevent regression:

  1. No route file under modules/ exceeds 600 lines
  2. Route files are organized (package or sibling pattern)
  3. Every routes/ package has an __init__.py that references `router`
  4. core/ has no route definitions (it's the shared kernel, not a domain)

Run without container: pytest tests/test_contracts/test_architecture_lint.py -v

See also: docs/adr/001-modular-architecture.md
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

MODULES_DIR = BACKEND_DIR / "modules"

# Hard limit for route files — no single route file may exceed this.
MAX_ROUTE_FILE_LINES = 600

# Soft limit for all files — warn but don't fail.
WARN_ALL_FILES_LINES = 800

# Modules that were split into routes/ packages in v1.4.1.
# These use the `routes/__init__.py` + sub-router pattern.
PACKAGE_ROUTE_MODULES = [
    "archives",
    "inventory",
    "jobs",
    "models_library",
    "notifications",
    "orders",
    "reporting",
    "vision",
]

# Modules that use the routes.py + routes_*.py sibling pattern.
# These were already split during the original refactor and are under 600L each.
SIBLING_ROUTE_MODULES = [
    "organizations",
    "printers",
    "system",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_route_file(path: Path) -> bool:
    """True if the file is a route definition (routes.py, routes_*.py, or inside routes/)."""
    name = path.name
    if name.startswith("routes") and name.endswith(".py"):
        return True
    if "routes" in path.parts:
        return True
    return False


def _route_files_in_modules():
    """Yield (relative_path, line_count) for every route .py file under modules/."""
    for py_file in sorted(MODULES_DIR.rglob("*.py")):
        if "__pycache__" in str(py_file):
            continue
        if not _is_route_file(py_file):
            continue
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        rel = py_file.relative_to(BACKEND_DIR)
        yield rel, len(lines)


def _all_py_files_in_modules():
    """Yield (relative_path, line_count) for every .py file under modules/."""
    for py_file in sorted(MODULES_DIR.rglob("*.py")):
        if "__pycache__" in str(py_file):
            continue
        try:
            lines = py_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        rel = py_file.relative_to(BACKEND_DIR)
        yield rel, len(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFileSizeLimits:
    """Route files must stay under MAX_ROUTE_FILE_LINES."""

    def test_no_oversized_route_files(self):
        """Hard fail: no route file may exceed the limit."""
        oversized = []
        for rel_path, count in _route_files_in_modules():
            if count > MAX_ROUTE_FILE_LINES:
                oversized.append(f"  {rel_path}: {count} lines (limit: {MAX_ROUTE_FILE_LINES})")

        assert not oversized, (
            f"{len(oversized)} route file(s) exceed the {MAX_ROUTE_FILE_LINES}-line limit.\n"
            "Split oversized route files into sub-routers or extract helpers.\n"
            "See docs/adr/001-modular-architecture.md for guidance.\n\n"
            + "\n".join(oversized)
        )

    def test_warn_large_non_route_files(self):
        """Informational — flag non-route files over WARN_ALL_FILES_LINES."""
        large = []
        for rel_path, count in _all_py_files_in_modules():
            if not _is_route_file(BACKEND_DIR / rel_path) and count > WARN_ALL_FILES_LINES:
                large.append(f"  {rel_path}: {count} lines")

        if large:
            import warnings
            warnings.warn(
                f"\n{len(large)} non-route file(s) exceed {WARN_ALL_FILES_LINES} lines "
                f"(not enforced, but consider splitting):\n"
                + "\n".join(large)
            )

    def test_warn_route_files_approaching_limit(self):
        """Informational — flag route files over 80% of the limit."""
        warn_threshold = int(MAX_ROUTE_FILE_LINES * 0.8)  # 480 lines
        approaching = []
        for rel_path, count in _route_files_in_modules():
            if warn_threshold < count <= MAX_ROUTE_FILE_LINES:
                approaching.append(f"  {rel_path}: {count} lines ({count}/{MAX_ROUTE_FILE_LINES})")

        if approaching:
            import warnings
            warnings.warn(
                f"\n{len(approaching)} route file(s) approaching the {MAX_ROUTE_FILE_LINES}-line limit:\n"
                + "\n".join(approaching)
            )


class TestPackageRouteStructure:
    """Modules split in v1.4.1 must use routes/ packages."""

    @pytest.mark.parametrize("module_name", PACKAGE_ROUTE_MODULES)
    def test_routes_is_package(self, module_name):
        """routes/ must be a directory, with no leftover monolithic routes.py."""
        module_dir = MODULES_DIR / module_name
        routes_dir = module_dir / "routes"
        routes_file = module_dir / "routes.py"

        assert routes_dir.is_dir(), (
            f"modules/{module_name}/routes/ must be a package (directory). "
            f"Found routes.py instead: {routes_file.exists()}"
        )
        assert not routes_file.exists(), (
            f"modules/{module_name}/routes.py still exists alongside routes/ package. "
            "Delete the monolithic file — the package replaces it."
        )

    @pytest.mark.parametrize("module_name", PACKAGE_ROUTE_MODULES)
    def test_routes_package_has_init(self, module_name):
        """routes/__init__.py must exist."""
        init_file = MODULES_DIR / module_name / "routes" / "__init__.py"
        assert init_file.exists(), (
            f"modules/{module_name}/routes/__init__.py is missing."
        )

    @pytest.mark.parametrize("module_name", PACKAGE_ROUTE_MODULES)
    def test_routes_init_exports_router(self, module_name):
        """routes/__init__.py must contain 'router' (file-based check, no import needed)."""
        init_file = MODULES_DIR / module_name / "routes" / "__init__.py"
        if not init_file.exists():
            pytest.skip(f"__init__.py missing for {module_name}")

        content = init_file.read_text(encoding="utf-8")
        assert "router" in content, (
            f"modules/{module_name}/routes/__init__.py does not reference `router`. "
            "It must assemble sub-routers and export a combined `router`."
        )

    @pytest.mark.parametrize("module_name", PACKAGE_ROUTE_MODULES)
    def test_routes_package_has_subrouters(self, module_name):
        """Each routes/ package must have at least one sub-router file besides __init__.py."""
        routes_dir = MODULES_DIR / module_name / "routes"
        if not routes_dir.is_dir():
            pytest.skip(f"routes/ not a directory for {module_name}")

        py_files = [f for f in routes_dir.iterdir()
                     if f.suffix == ".py" and f.name != "__init__.py" and not f.name.startswith("_")]
        assert len(py_files) >= 1, (
            f"modules/{module_name}/routes/ has no sub-router files."
        )


class TestSiblingRouteStructure:
    """Modules using the routes.py + routes_*.py pattern must have split routes."""

    @pytest.mark.parametrize("module_name", SIBLING_ROUTE_MODULES)
    def test_has_route_files(self, module_name):
        """Module must have at least a routes.py and one routes_*.py sibling."""
        module_dir = MODULES_DIR / module_name
        routes_main = module_dir / "routes.py"
        route_siblings = list(module_dir.glob("routes_*.py"))

        assert routes_main.exists(), (
            f"modules/{module_name}/routes.py is missing."
        )
        assert len(route_siblings) >= 1, (
            f"modules/{module_name}/ has routes.py but no routes_*.py siblings. "
            "Route logic should be split across focused files."
        )


class TestCoreHasNoRoutes:
    """core/ is the shared kernel — it must not contain route definitions."""

    def test_no_router_in_core(self):
        core_dir = BACKEND_DIR / "core"
        if not core_dir.is_dir():
            pytest.skip("core/ directory not found")

        violations = []
        for py_file in sorted(core_dir.rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if "APIRouter(" in content:
                rel = py_file.relative_to(BACKEND_DIR)
                violations.append(str(rel))

        assert not violations, (
            "core/ must not contain route definitions (APIRouter). "
            "Route logic belongs in domain modules.\n"
            "Files with APIRouter: " + ", ".join(violations)
        )
