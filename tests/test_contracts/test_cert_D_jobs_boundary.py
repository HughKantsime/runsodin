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


def _internal_imports(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif (isinstance(node, ast.Call) and ast.unparse(node.func) in ("importlib.import_module", "import_module", "__import__")
              and node.args and isinstance(node.args[0], ast.Constant)):
            names = [str(node.args[0].value)]
        else:
            continue
        found += [(name, node.lineno) for name in names if name.startswith(INTERNALS)]
    return found


def test_only_the_jobs_module_imports_its_internals():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("backend/modules/jobs/") or "preserved027" in rel:
            continue
        offenders += [(rel, name, line) for name, line in _internal_imports(path.read_text(encoding="utf-8"))]
    assert offenders == [], offenders


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
])
def test_scanner_detects_each_import_form(planted):
    assert _internal_imports(planted)


def test_scanner_allows_the_boundary_and_shared_types():
    assert not _internal_imports("from modules.jobs.services import transition_job\nfrom modules.jobs.models import Job")
