"""cert-D: the jobs module's internals are reached only through modules.jobs.services.

test_no_cross_module_imports covers backend/modules only and allows several
patterns; this test covers every backend file (core included) and names the
jobs internals explicitly, whatever the import form.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
INTERNALS = ("modules.jobs.state_machine", "modules.jobs.pinning")


def _module_of(rel):
    """Dotted module name of a backend file (backend/ is the import root)."""
    parts = rel.removeprefix("backend/").removesuffix(".py").split("/")
    return parts[:-1] if parts[-1] == "__init__" else parts


def _internal_imports(source, rel="backend/modules/planted.py"):
    from tests.test_contracts.job_census import DYNAMIC, _string_value
    found = []
    package = _module_of(rel)
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative: resolve against this file's package
                anchor = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join([*anchor, *([base] if base else [])])
            names = [base] + [f"{base}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] in ("import_module", "__import__") and node.args:
            target = _string_value(node.args[0])
            if target is None:  # a wholly computed name: allowed only per site (COMPUTED_IMPORTS)
                found.append((f"modules.jobs.{DYNAMIC}", node.lineno, ast.unparse(node.args[0])))
                continue
            if target.startswith(".") and len(node.args) > 1:  # import_module('.x', 'modules.jobs')
                target = (_string_value(node.args[1]) or DYNAMIC) + target
            if DYNAMIC in target and target.split(DYNAMIC)[0] in ("", "modules.", "modules.jobs", "modules.jobs."):
                names = [f"modules.jobs.{DYNAMIC}"]   # may name a jobs internal: refuse
            else:
                names = [target]
        else:
            continue
        found += [(name, node.lineno, None) for name in names
                  if name.startswith(INTERNALS) or name == f"modules.jobs.{DYNAMIC}"]
    return found


def _source(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _migrations_never_name_jobs_internals():
    import importlib
    allowed = importlib.import_module("core.schema.migrator")._ALLOWED_PYTHON_MIGRATIONS
    source = _source("backend/core/schema/migrator.py")
    return (not any(name.startswith(INTERNALS) or name.startswith("modules.jobs") for name in allowed)
            and "if module_name not in _ALLOWED_PYTHON_MIGRATIONS:" in source)


# Imports whose module name is wholly computed: allowed per (file, expression),
# with an exact count and a check that the names cannot be jobs internals.
COMPUTED_IMPORTS = {
    ("backend/core/app_maintenance.py", "pkg_name"): (1, "module discovery imports package roots modules.<name>",
        lambda: "pkg_name = f'modules.{entry.name}'" in ast.unparse(ast.parse(_source("backend/core/app_maintenance.py")))),
    ("backend/core/app_maintenance.py", "pkg"): (1, "load order visits only supplied discovered roots",
        lambda: "for pkg in pkg_names:" in ast.unparse(ast.parse(_source("backend/core/app_maintenance.py")))),
    ("backend/core/app.py", "pkg"): (1, "registration iterates the discovered package roots",
        lambda: all(fragment in ast.unparse(ast.parse(_source("backend/core/app.py"))) for fragment in (
            "pkg_names = _discover_modules()", "ordered_pkgs = _resolve_load_order(pkg_names)"))),
    ("backend/core/schema/migrator.py", "module_name"): (1, "Python migrations from a closed allowlist",
        _migrations_never_name_jobs_internals),
}


def test_only_the_jobs_module_imports_its_internals():
    offenders, computed = [], {}
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("backend/modules/jobs/") or "preserved027" in rel or "preserved029/bundle/" in rel or "preserved034/bundle/" in rel or "preserved035/overlay/" in rel or "preserved036/overlay/" in rel or "preserved037/overlay/" in rel:
            continue
        for name, line, expression in _internal_imports(path.read_text(encoding="utf-8"), rel):
            if expression is not None and (rel, expression) in COMPUTED_IMPORTS:
                computed[(rel, expression)] = computed.get((rel, expression), 0) + 1
            else:
                offenders.append((rel, name, line, expression))
    assert offenders == [], offenders
    assert computed == {key: count for key, (count, _, _) in COMPUTED_IMPORTS.items()}, computed
    for key, (_, reason, check) in COMPUTED_IMPORTS.items():
        assert check(), (key, reason)


def test_the_services_boundary_exposes_what_other_modules_use():
    from modules.jobs import services
    assert {"transition_job", "transition_result", "Expr", "TransitionError",
            "clear_pins_for_deleted_printer"} <= set(services.__all__)


@pytest.mark.parametrize("planted", [
    "from modules.jobs.state_machine import transition_job",
    "import modules.jobs.pinning",
    "from modules.jobs import state_machine",
    "def f():\n    from modules.jobs.pinning import pin_columns",
    "import importlib\nm = importlib.import_module('modules.jobs.state_machine')",
    "m = importlib.import_module('modules.jobs.' + 'pinning')",
    "m = importlib.import_module(f'modules.jobs.{name}')",
    "m = __import__('modules.jobs.state_machine', fromlist=['transition_job'])",
    "m = importlib.import_module('.state_machine', 'modules.jobs')",
    "m = importlib.import_module(name)",
])
def test_scanner_detects_each_import_form(planted):
    assert _internal_imports(planted)


@pytest.mark.parametrize("rel,planted", [
    ("backend/modules/printers/routes_x.py", "from ..jobs.state_machine import transition_job"),
    ("backend/modules/printers/monitors/x.py", "from ...jobs import pinning"),
    ("backend/modules/printers/__init__.py", "from ..jobs.pinning import pin_columns"),
])
def test_scanner_resolves_relative_imports(rel, planted):
    assert _internal_imports(planted, rel)


def test_relative_imports_inside_the_jobs_module_resolve_to_itself():
    assert _internal_imports("from .state_machine import transition_job", "backend/modules/jobs/routes/x.py") == []
    assert _internal_imports("from ..state_machine import transition_job", "backend/modules/jobs/routes/x.py")


def test_scanner_allows_the_boundary_and_shared_types():
    assert not _internal_imports("from modules.jobs.services import transition_job\nfrom modules.jobs.models import Job")


@pytest.mark.parametrize("era", ["035", "036"])
def test_preserved_overlay_scanner_exemption_is_integrity_checked(era):
    # The exempt overlay is immutable historical input, just like preserved034.
    from importlib import import_module
    check_package = import_module(f"core.schema.preserved{era}.integrity").check_package
    bundle, manifest = check_package()
    assert (bundle / 'core/schema/migrator.py').is_file()
    assert 'core/schema/migrator.py' in manifest['sources']
