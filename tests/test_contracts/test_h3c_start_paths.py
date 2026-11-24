"""Inventory of application Bambu start commands and dispatch permissions."""
import ast
from pathlib import Path


def test_application_project_file_and_allow_print_start_sites():
    backend = Path(__file__).resolve().parents[2] / 'backend'
    sites = {'project_file': [], 'allow_print_start': []}
    for path in backend.rglob('*.py'):
        relative = path.relative_to(backend)
        if 'tests' in relative.parts or relative.as_posix().startswith('core/schema/preserved'):
            continue
        source = path.read_text()
        if not any(name in source for name in sites):
            continue
        tree = ast.parse(source)
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            # Count literal command names and permission keys, including **dict arguments.
            if isinstance(node, ast.Constant):
                name = node.value
            elif isinstance(node, ast.keyword):
                name = node.arg
            else:
                continue
            if name in sites:
                owners = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
                owner = max(owners, key=lambda f: f.lineno).name if owners else '<module>'
                sites[name].append((relative.as_posix(), owner))

    legacy = 'modules/printers/adapters/bambu.py'
    v2 = 'modules/printers/telemetry/bambu/commands.py'
    dispatch = 'modules/printers/dispatch.py'
    guard = 'modules/printers/printer_models.py'
    assert sorted(sites['project_file']) == sorted([
        (legacy, 'start_print'), (v2, 'start_print'),
        (guard, 'stored_rack_refusal'), (guard, 'stored_rack_refusal'),
        ('modules/printers/telemetry/bambu/project_reply.py', 'observe'),
    ])
    assert sorted(sites['allow_print_start']) == sorted([
        (dispatch, '_dispatch_bambu_legacy'), (dispatch, '_dispatch_bambu_v2'),
        (legacy, '_publish'), (v2, '_publish'),
    ])
