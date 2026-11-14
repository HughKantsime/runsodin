"""The health split preserves the eleven license-management endpoints."""

import ast
from pathlib import Path

from modules.system import routes_health, routes_license


def test_license_routes_live_only_in_license_router():
    expected = {
        ("GET", "/license"),
        ("GET", "/license/last-refresh"),
        ("POST", "/license/upload"),
        ("DELETE", "/license"),
        ("GET", "/license/installation-id"),
        ("POST", "/license/activate"),
        ("POST", "/license/trial-request"),
        ("POST", "/license/activation-request"),
        ("POST", "/license/unactivate"),
        ("POST", "/license/reactivate"),
        ("POST", "/license/unactivation-request"),
    }
    routes = routes_license.router.routes
    pairs = [(method, route.path) for route in routes for method in route.methods]
    assert set(pairs) == expected
    assert len(pairs) == len(expected)
    assert all(route.endpoint.__module__ == routes_license.__name__ for route in routes)
    assert not any(route.path.startswith("/license") for route in routes_health.router.routes)

    health_tree = ast.parse(Path(routes_health.__file__).read_text())
    health_functions = {
        node.name for node in health_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not health_functions.intersection(route.endpoint.__name__ for route in routes)
    assert "_fetch_license_challenge" not in health_functions
