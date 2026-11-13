"""The license refresh routes live in routes_license.py; their wire is unchanged.

routes_health.py went over the route file size limit (test_architecture_lint).
The move must not change a path, a method, a guard or where the route is served.
"""
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute

from license_manager import require_license_mutation_enabled
from modules.system import routes, routes_health, routes_license

MOVED = {
    ("GET", "/license/last-refresh"): "get_license_last_refresh",
    ("POST", "/license/reactivate"): "reactivate_license",
}


def _routes(router):
    return {(method, route.path): route for route in router.routes if isinstance(route, APIRoute)
            for method in route.methods}


def test_refresh_routes_are_served_once_at_the_same_paths():
    served = [(method, route.path) for route in routes.router.routes if isinstance(route, APIRoute)
              for method in route.methods]
    table = _routes(routes.router)
    for key, name in MOVED.items():
        assert served.count(key) == 1, key
        assert table[key].endpoint is getattr(routes_license, name)
        assert table[key].tags == ["License"]
        assert key not in _routes(routes_health.router)
        assert not hasattr(routes_health, name)
    assert set(_routes(routes_license.router)) == set(MOVED)


def test_refresh_routes_keep_their_guards():
    def guards(endpoint):
        import inspect
        return {name: parameter.default.dependency for name, parameter in inspect.signature(endpoint).parameters.items()
                if isinstance(parameter.default, DependsParam)}

    refresh = guards(routes_license.reactivate_license)
    assert set(refresh) == {"current_user", "db", "license_mutation_allowed"}
    assert refresh["license_mutation_allowed"] is require_license_mutation_enabled
    # Same guard objects as the license routes that stayed behind.
    stayed = guards(routes_health.unactivate_license)
    assert refresh["db"] is stayed["db"]
    assert refresh["current_user"].__qualname__ == stayed["current_user"].__qualname__
    last = guards(routes_license.get_license_last_refresh)
    assert set(last) == {"current_user"}
    assert last["current_user"].__qualname__ == guards(routes_health.upload_license)["current_user"].__qualname__


def test_the_refresh_router_is_registered_with_the_system_module():
    import modules.system as system
    assert "system.routes_license" in system.ROUTES
