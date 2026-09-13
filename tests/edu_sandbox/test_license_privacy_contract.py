"""Contracts for the EDU sandbox's public and authenticated license evidence."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "backend" / "modules" / "system" / "routes_health.py"


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing route handler {name}")


def test_public_license_route_uses_redacted_projection() -> None:
    source = ast.unparse(_function("get_license_info"))
    assert ".to_public_dict()" in source
    assert ".to_dict()" not in source


def test_installation_identity_requires_admin_role() -> None:
    source = ast.unparse(_function("get_license_installation_id"))
    assert "require_role('admin')" in source


def test_runtime_reads_installation_identity_with_admin_token() -> None:
    source = (ROOT / "ops" / "edu_sandbox" / "runtime.py").read_text(encoding="utf-8")
    assert '"/api/license/installation-id"' in source
    assert 'token=tokens["administrator"]' in source
