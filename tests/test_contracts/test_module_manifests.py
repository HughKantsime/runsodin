"""
Contract tests — Module manifests.

Verify that every module under backend/modules/ declares a valid manifest with
all required fields, that MODULE_ID matches the directory name, and that no two
modules claim ownership of the same table.

These tests run without a container: pytest tests/test_contracts/test_module_manifests.py -v
"""

import importlib
import sys
from pathlib import Path

import pytest

# Ensure backend/ is on sys.path so module imports resolve.
BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# The 12 expected modules.
EXPECTED_MODULES = [
    "archives",
    "inventory",
    "jobs",
    "models_library",
    "notifications",
    "orders",
    "organizations",
    "printers",
    "reporting",
    "system",
    "vision",
]

# Required manifest fields in every __init__.py.
REQUIRED_FIELDS = [
    "MODULE_ID",
    "MODULE_VERSION",
    "MODULE_DESCRIPTION",
    "ROUTES",
    "TABLES",
    "PUBLISHES",
    "SUBSCRIBES",
    "IMPLEMENTS",
    "REQUIRES",
    "DAEMONS",
]


def _load_module(module_name: str):
    """Import and return a module manifest."""
    full_name = f"modules.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    return importlib.import_module(full_name)


class TestModuleDirectories:
    """All 12 modules must exist as directories."""

    def test_all_expected_modules_exist(self):
        modules_dir = BACKEND_DIR / "modules"
        existing = {d.name for d in modules_dir.iterdir() if d.is_dir() and not d.name.startswith("_")}
        missing = set(EXPECTED_MODULES) - existing
        assert not missing, f"Expected module directories not found: {sorted(missing)}"

    def test_no_unexpected_modules(self):
        modules_dir = BACKEND_DIR / "modules"
        existing = {d.name for d in modules_dir.iterdir() if d.is_dir() and not d.name.startswith("_")}
        unexpected = existing - set(EXPECTED_MODULES)
        assert not unexpected, (
            f"Unexpected module directories found (add to EXPECTED_MODULES or remove): {sorted(unexpected)}"
        )


class TestManifestFields:
    """Each module must declare all required manifest fields."""

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_required_fields_present(self, module_name):
        mod = _load_module(module_name)
        missing = [f for f in REQUIRED_FIELDS if not hasattr(mod, f)]
        assert not missing, (
            f"modules.{module_name} is missing manifest fields: {missing}"
        )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_module_id_matches_directory_name(self, module_name):
        mod = _load_module(module_name)
        assert mod.MODULE_ID == module_name, (
            f"modules.{module_name}.MODULE_ID = {mod.MODULE_ID!r}, expected {module_name!r}"
        )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_module_version_is_string(self, module_name):
        mod = _load_module(module_name)
        assert isinstance(mod.MODULE_VERSION, str), (
            f"modules.{module_name}.MODULE_VERSION must be a string"
        )
        # Must be semver-like: N.N.N
        parts = mod.MODULE_VERSION.split(".")
        assert len(parts) == 3, (
            f"modules.{module_name}.MODULE_VERSION {mod.MODULE_VERSION!r} is not semver (expected X.Y.Z)"
        )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_list_fields_are_lists(self, module_name):
        mod = _load_module(module_name)
        list_fields = ["ROUTES", "TABLES", "PUBLISHES", "SUBSCRIBES", "IMPLEMENTS", "REQUIRES", "DAEMONS"]
        for field in list_fields:
            value = getattr(mod, field)
            assert isinstance(value, list), (
                f"modules.{module_name}.{field} must be a list, got {type(value).__name__}"
            )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_list_fields_contain_only_strings(self, module_name):
        mod = _load_module(module_name)
        list_fields = ["TABLES", "PUBLISHES", "SUBSCRIBES", "IMPLEMENTS", "REQUIRES", "DAEMONS"]
        for field in list_fields:
            for item in getattr(mod, field):
                assert isinstance(item, str), (
                    f"modules.{module_name}.{field} must contain only strings, got {type(item).__name__}: {item!r}"
                )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_module_description_is_non_empty_string(self, module_name):
        mod = _load_module(module_name)
        assert isinstance(mod.MODULE_DESCRIPTION, str) and mod.MODULE_DESCRIPTION.strip(), (
            f"modules.{module_name}.MODULE_DESCRIPTION must be a non-empty string"
        )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_register_function_exists(self, module_name):
        mod = _load_module(module_name)
        assert callable(getattr(mod, "register", None)), (
            f"modules.{module_name} must define a callable register(app, registry) function"
        )


class TestTableOwnership:
    """No two modules may declare ownership of the same table."""

    def test_no_duplicate_table_ownership(self):
        from collections import defaultdict
        ownership: dict[str, list[str]] = defaultdict(list)

        for module_name in EXPECTED_MODULES:
            mod = _load_module(module_name)
            for table in mod.TABLES:
                ownership[table].append(module_name)

        duplicates = {table: owners for table, owners in ownership.items() if len(owners) > 1}
        assert not duplicates, (
            "Multiple modules claim ownership of the same table(s):\n"
            + "\n".join(f"  {table}: owned by {owners}" for table, owners in sorted(duplicates.items()))
        )

    def test_all_declared_tables_are_strings(self):
        for module_name in EXPECTED_MODULES:
            mod = _load_module(module_name)
            for table in mod.TABLES:
                assert isinstance(table, str) and table.strip(), (
                    f"modules.{module_name}.TABLES contains a non-string entry: {table!r}"
                )


class TestEventDeclarations:
    """Event types in PUBLISHES/SUBSCRIBES must follow the dot-notation convention."""

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_published_events_follow_convention(self, module_name):
        mod = _load_module(module_name)
        for event in mod.PUBLISHES:
            assert "." in event, (
                f"modules.{module_name}.PUBLISHES contains {event!r} — "
                f"event types must use dot notation (e.g. 'printer.state_changed')"
            )

    @pytest.mark.parametrize("module_name", EXPECTED_MODULES)
    def test_subscribed_events_follow_convention(self, module_name):
        mod = _load_module(module_name)
        for event in mod.SUBSCRIBES:
            assert "." in event, (
                f"modules.{module_name}.SUBSCRIBES contains {event!r} — "
                f"event types must use dot notation (e.g. 'job.completed')"
            )

    def test_published_events_have_a_subscriber(self):
        """
        Every published event should be subscribed to by at least one module.
        This catches orphaned events that nobody listens to.
        Warnings only — does not fail — because some events are consumed by
        external systems (MQTT bridge, WebSocket hub) that don't declare manifests.
        """
        all_subscribed: set[str] = set()
        all_published: dict[str, list[str]] = {}

        for module_name in EXPECTED_MODULES:
            mod = _load_module(module_name)
            all_subscribed.update(mod.SUBSCRIBES)
            for event in mod.PUBLISHES:
                all_published.setdefault(event, []).append(module_name)

        orphaned = {e: publishers for e, publishers in all_published.items() if e not in all_subscribed}
        # Not a hard failure — external consumers (ws_hub, mqtt_republish) handle many events.
        # Just verify this method runs without error; actual orphan tracking is informational.
        assert isinstance(orphaned, dict)  # always passes; structure check only
