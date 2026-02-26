"""
Contract tests — Module boundary enforcement.

THE MOST IMPORTANT TEST IN THIS SUITE.

Scans all Python files under backend/modules/ for imports that reference
another module's *route logic or service functions* directly. This catches
the worst form of coupling: route handlers calling into another module's
route handlers, bypassing the interface layer.

What is ALLOWED (in the allowlist):
- Importing from another module's models.py  (shared data types)
- Importing from another module's schemas.py (shared Pydantic types)
- Importing adapters from printers/ (hardware drivers used by other modules)
- Importing event_dispatcher / mqtt_republish from notifications/ (the event layer)
- Importing utility functions (hms_codes, threemf_parser, quiet_hours, branding,
  printer_models, route_utils) — these are shared utilities, not route logic

What is FLAGGED as a violation:
- Importing from another module's routes.py, routes_*.py files
  (route logic coupling — the function belongs in a shared service or interface)

Run without container: pytest tests/test_contracts/test_no_cross_module_imports.py -v
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

MODULES_DIR = BACKEND_DIR / "modules"


# ---------------------------------------------------------------------------
# Allowlist — known legitimate cross-module imports.
#
# Format: each entry is a substring that, if present in an import line,
# exempts that line from being flagged as a violation.
#
# Add entries here when a cross-module dependency is intentional and reviewed.
# Each entry should have a comment explaining WHY it is allowed.
# ---------------------------------------------------------------------------

ALLOWED_PATTERNS = [
    # Shared data types: modules read from each other's models and schemas.
    # This is the intended pattern — models are "shared read-only state".
    ".models import",
    ".schemas import",

    # Printer adapters (hardware drivers) are used by:
    #   - vision/monitor.py  (captures frames from printer cameras)
    #   - system/profile_routes.py (slicer profile compatibility check)
    #   - system/routes_setup.py  (test-connection during setup wizard)
    ".adapters.",

    # The event dispatcher and MQTT republish bridge are the notification
    # integration layer. Monitors publish events through these — this is
    # the intended architecture for cross-module communication.
    ".event_dispatcher",
    ".mqtt_republish",
    ".alert_dispatcher",

    # Data/utility files — pure functions with no route logic side effects.
    ".hms_codes",         # Bambu HMS error code lookup (data file)
    ".quiet_hours",       # Quiet hours suppression helper
    ".branding",          # Org branding helper (read-only config)
    ".printer_models",    # Printer model metadata (data file)
    ".threemf_parser",    # 3MF file parsing utility
    ".route_utils",       # Shared SSRF blocklist, go2rtc config sync
    ".dispatch import",   # Printer dispatch (send job to printer HW)
]

# ---------------------------------------------------------------------------
# Known violations — pre-existing coupling that should be refactored.
#
# These are GENUINE boundary violations that were not resolved during the
# modular architecture refactor. They are tracked here to prevent new
# violations from being silently added. Each entry has a remediation note.
#
# DO NOT add new entries without code review. Instead, fix the violation.
# ---------------------------------------------------------------------------

KNOWN_VIOLATIONS = [
    # jobs/routes.py imports calculate_job_cost from models_library/routes.py.
    # Remediation: Extract calculate_job_cost to modules/models_library/services.py
    # and import from there, or expose it via the registry.
    "modules.models_library.routes import calculate_job_cost",

    # jobs/routes.py and notifications/alert_dispatcher.py import _get_org_settings
    # from organizations/routes.py. Remediation: Use the OrgSettingsProvider interface
    # registered in the registry, or extract to modules/organizations/services.py.
    "modules.organizations.routes import _get_org_settings",
]


def _is_allowed(import_line: str) -> bool:
    """Return True if the import line matches an allowed pattern or known violation."""
    if any(pattern in import_line for pattern in ALLOWED_PATTERNS):
        return True
    if any(kv in import_line for kv in KNOWN_VIOLATIONS):
        return True
    return False


def _find_violations() -> list[str]:
    """
    Scan all .py files under backend/modules/ for cross-module imports that
    are not in the allowlist.

    A "cross-module import" is any line starting with 'from modules.' or
    'import modules.' that references a module other than the file's own module.
    """
    violations = []

    for module_dir in sorted(MODULES_DIR.iterdir()):
        if not module_dir.is_dir() or module_dir.name.startswith("_"):
            continue

        own_module = module_dir.name

        for py_file in sorted(module_dir.rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue

            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue

            for lineno, raw_line in enumerate(lines, start=1):
                stripped = raw_line.strip()

                # Only check lines that start a cross-module import.
                if not stripped.startswith(("from modules.", "import modules.")):
                    continue

                # Extract the referenced module name.
                m = re.search(r'(?:from|import)\s+modules\.([a-z_]+)', stripped)
                if not m:
                    continue

                ref_module = m.group(1)

                # Self-imports are fine.
                if ref_module == own_module:
                    continue

                # Check the allowlist.
                if _is_allowed(stripped):
                    continue

                # Violation found.
                rel_path = py_file.relative_to(BACKEND_DIR.parent)
                violations.append(f"{rel_path}:{lineno}: {stripped}")

    return violations


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

class TestNoCrossModuleImports:
    """
    Enforce module boundary contracts.

    Any import that is neither a self-import nor in the allowlist is a
    violation of module boundaries and must be refactored to go through:
      - core/interfaces/ (for interface-based access)
      - core/events.py   (for event-driven communication)
    """

    def test_no_direct_cross_module_route_imports(self):
        """
        No module may import route logic or service functions directly from
        another module's routes files. Use interfaces or events instead.
        """
        violations = _find_violations()
        assert not violations, (
            f"Cross-module route/service imports found ({len(violations)} violation(s)).\n"
            "These create direct coupling between modules. Refactor to use:\n"
            "  - core/interfaces/ for synchronous service access\n"
            "  - core/events.py for asynchronous event-driven communication\n\n"
            "Violations:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_allowlist_patterns_are_non_empty_strings(self):
        """Sanity check: every allowlist entry is a non-empty string."""
        for pattern in ALLOWED_PATTERNS:
            assert isinstance(pattern, str) and pattern.strip(), (
                f"Allowlist contains an invalid entry: {pattern!r}"
            )

    def test_modules_directory_exists(self):
        """The modules directory must exist for this test to be meaningful."""
        assert MODULES_DIR.is_dir(), f"backend/modules/ not found at {MODULES_DIR}"

    def test_at_least_one_module_scanned(self):
        """Verify the scanner actually found module directories to scan."""
        modules = [d for d in MODULES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")]
        assert len(modules) >= 10, (
            f"Expected at least 10 module directories, found {len(modules)}: "
            f"{[d.name for d in modules]}"
        )

    def test_known_violations_still_exist(self):
        """
        Verify that the known violations in KNOWN_VIOLATIONS actually appear
        in the codebase. If they disappear (fixed), remove them from KNOWN_VIOLATIONS
        so the list stays accurate.
        """
        all_lines = []
        for module_dir in MODULES_DIR.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_"):
                continue
            for py_file in module_dir.rglob("*.py"):
                if "__pycache__" in str(py_file):
                    continue
                try:
                    all_lines.extend(py_file.read_text(encoding="utf-8").splitlines())
                except Exception:
                    pass

        stale = []
        for kv in KNOWN_VIOLATIONS:
            found = any(kv in line for line in all_lines)
            if not found:
                stale.append(kv)

        assert not stale, (
            "These entries in KNOWN_VIOLATIONS no longer appear in the codebase.\n"
            "They have been fixed — remove them from KNOWN_VIOLATIONS:\n"
            + "\n".join(f"  {kv}" for kv in stale)
        )


# ---------------------------------------------------------------------------
# Informational: report all cross-module imports (allowed + violations)
# ---------------------------------------------------------------------------

def test_cross_module_import_inventory():
    """
    Non-failing inventory of all cross-module imports.

    This test always passes. Its purpose is to make the full dependency graph
    visible in the test output (use -v or -s to see it).
    """
    all_imports: dict[str, list[str]] = {}

    for module_dir in sorted(MODULES_DIR.iterdir()):
        if not module_dir.is_dir() or module_dir.name.startswith("_"):
            continue

        own_module = module_dir.name

        for py_file in sorted(module_dir.rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue

            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue

            for raw_line in lines:
                stripped = raw_line.strip()
                if not stripped.startswith(("from modules.", "import modules.")):
                    continue

                m = re.search(r'(?:from|import)\s+modules\.([a-z_]+)', stripped)
                if not m:
                    continue

                ref_module = m.group(1)
                if ref_module == own_module:
                    continue

                key = f"{own_module} -> {ref_module}"
                all_imports.setdefault(key, []).append(stripped)

    # Always passes — just a structured inventory.
    assert isinstance(all_imports, dict)
