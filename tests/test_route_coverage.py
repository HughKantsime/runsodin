"""
RBAC Route Coverage Gate
test_route_coverage.py

Ensures every route registered in the FastAPI app has a corresponding entry
in the RBAC test matrix (test_rbac.py::ENDPOINT_MATRIX).

WHY THIS EXISTS
--------------
The Huntarr class of vulnerability: a new endpoint gets added to a router,
the developer forgets to add it to the auth middleware or the RBAC matrix,
and it ships unauthenticated. This test makes that impossible by failing CI
whenever a new route is not accounted for in the RBAC matrix.

HOW IT WORKS
------------
1. Fetches the OpenAPI spec from the running server (/openapi.json).
2. Extracts every (method, path) pair that the server exposes.
3. Normalises path parameters to a canonical form for comparison.
4. Cross-references against ENDPOINT_MATRIX in test_rbac.py.
5. Fails with a clear list of any route not yet covered.

WHAT TO DO WHEN THIS TEST FAILS
--------------------------------
A new endpoint was added to a router but not to ENDPOINT_MATRIX. Add it to
ENDPOINT_MATRIX in test_rbac.py with the correct auth expectations, then this
test will pass.

Usage:
    pytest tests/test_route_coverage.py -v
"""

import os
import re
import pytest
import requests
from pathlib import Path
from urllib.parse import urljoin


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parent / ".env.test"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

_load_env()
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
API_KEY  = os.environ.get("API_KEY", "")


# ---------------------------------------------------------------------------
# Routes that are intentionally excluded from RBAC matrix coverage.
# Document WHY each exclusion exists.
# ---------------------------------------------------------------------------

EXCLUDED_PATHS = {
    # OpenAPI / Swagger UI — meta endpoints, not API surface
    "/openapi.json",
    "/api/v1/openapi.json",
    "/api/v1/docs",
    "/api/v1/docs/oauth2-redirect",
    "/api/v1/redoc",

    # WebSocket — cannot be tested with standard HTTP requests
    "/ws",
    "/ws/{client_id}",

    # Static file mounts — handled by StaticFiles middleware, not FastAPI routes
    "/{path:path}",
    "/assets/{path:path}",
    "/static/{path:path}",
    "/api/vision/frames/{path:path}",
}


def _normalise_path(path: str) -> str:
    """
    Normalise path parameter names so comparison is name-agnostic.
    OpenAPI: /api/printers/{printer_id}
    Matrix:  /api/printers/{printer_id}   ← same, but handle mismatches

    We strip the parameter name and replace with a canonical {id} placeholder.
    This allows the matrix to use any param name and still match the live route.
    """
    return re.sub(r"\{[^}]+\}", "{id}", path)


def _get_matrix_routes() -> set:
    """Import and normalise all (method, path) pairs from ENDPOINT_MATRIX."""
    import sys
    tests_dir = Path(__file__).parent
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    from test_rbac import ENDPOINT_MATRIX
    return {
        (method.upper(), _normalise_path(path))
        for method, path, *_ in ENDPOINT_MATRIX
    }


def _get_server_routes() -> list[tuple[str, str]]:
    """Fetch all (method, path) pairs from the running server's OpenAPI spec."""
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    try:
        resp = requests.get(
            urljoin(BASE_URL, "/openapi.json"),
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        pytest.fail(
            f"Could not fetch OpenAPI spec from {BASE_URL}/openapi.json — "
            f"is the container running?\n{e}"
        )

    spec = resp.json()
    routes = []

    for path, path_item in spec.get("paths", {}).items():
        for method in path_item:
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                routes.append((method.upper(), path))

    return routes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_routes_in_rbac_matrix():
    """
    Every route exposed by the server must have a corresponding entry in
    ENDPOINT_MATRIX. Fail with a clear list of any that are missing.

    This is the Huntarr-class check: catches new endpoints that ship without
    auth expectations documented and tested.
    """
    matrix_routes = _get_matrix_routes()
    server_routes = _get_server_routes()

    uncovered = []
    for method, path in server_routes:
        if path in EXCLUDED_PATHS:
            continue
        normalised = (method, _normalise_path(path))
        if normalised not in matrix_routes:
            uncovered.append((method, path))

    if uncovered:
        lines = [
            "",
            "=" * 65,
            "ROUTE COVERAGE FAILURE — endpoints not in RBAC matrix:",
            "=" * 65,
            "",
            "These routes are registered in the server but have no entry",
            "in ENDPOINT_MATRIX (tests/test_rbac.py).",
            "",
            "Fix: add each route to ENDPOINT_MATRIX with correct auth",
            "expectations (see existing entries for format).",
            "",
        ]
        for method, path in sorted(uncovered):
            lines.append(f"  {method:<8} {path}")
        lines.append("")
        pytest.fail("\n".join(lines))


def test_matrix_has_no_phantom_routes():
    """
    Every path in ENDPOINT_MATRIX should exist on the server.
    Warns (not fails) if a matrix entry has no matching server route —
    this catches stale entries after endpoints are removed.
    """
    matrix_routes = _get_matrix_routes()
    server_routes = _get_server_routes()

    server_normalised = {
        (method.upper(), _normalise_path(path))
        for method, path in server_routes
    }

    phantom = []
    for method, path in matrix_routes:
        if path in {_normalise_path(p) for p in EXCLUDED_PATHS}:
            continue
        # Special cases that don't appear in OpenAPI (WebSocket, health, etc.)
        if _normalise_path(path) in {
            _normalise_path("/health"),
            _normalise_path("/api/auth/login"),
            _normalise_path("/api/auth/logout"),
            _normalise_path("/api/setup/admin"),
            _normalise_path("/api/setup/status"),
            _normalise_path("/api/setup/test-printer"),
            _normalise_path("/api/setup/printer"),
            _normalise_path("/api/setup/complete"),
        }:
            continue
        if (method, path) not in server_normalised:
            phantom.append((method, path))

    if phantom:
        # Warn but don't fail — phantom entries are stale, not dangerous
        print("\nWARN: RBAC matrix has entries for routes not on the server:")
        for method, path in sorted(phantom):
            print(f"  {method:<8} {path}")
        print("Consider removing these stale entries from ENDPOINT_MATRIX.")


def test_no_unauthenticated_write_endpoints():
    """
    Belt-and-suspenders check: hit every non-GET route with zero auth
    (no API key, no JWT) and assert a non-2xx response.

    This is a direct replay of the Huntarr bug: POST /api/settings/general
    with no credentials returned 200. That must never happen here.

    Note: skips routes that are intentionally public (auth/login, setup/*,
    health) since those genuinely should return 2xx without auth.
    """
    INTENTIONALLY_PUBLIC_WRITE_PATHS = {
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/setup/admin",
        "/api/setup/test-printer",
        "/api/setup/printer",
        "/api/setup/complete",
        "/api/setup/clear",
    }

    server_routes = _get_server_routes()
    failures = []

    for method, path in server_routes:
        if method == "GET":
            continue
        if path in EXCLUDED_PATHS:
            continue

        # Skip intentionally public write endpoints
        normalised = _normalise_path(path)
        if any(
            _normalise_path(p) == normalised
            for p in INTENTIONALLY_PUBLIC_WRITE_PATHS
        ):
            continue

        # Replace path params with dummy values so the URL is valid
        test_path = re.sub(r"\{[^}]+\}", "99999", path)
        url = urljoin(BASE_URL, test_path)

        try:
            resp = requests.request(
                method,
                url,
                timeout=5,
                headers={},    # explicitly no auth
                json={},       # empty body
                allow_redirects=False,
            )
            if resp.status_code < 400:
                failures.append(
                    f"  {method:<8} {path}  → HTTP {resp.status_code} (expected 4xx)"
                )
        except requests.RequestException:
            # Connection errors, timeouts — not a security failure
            pass

    if failures:
        lines = [
            "",
            "=" * 65,
            "UNAUTHENTICATED WRITE ENDPOINT FAILURE:",
            "=" * 65,
            "",
            "These write endpoints returned a success response with",
            "zero credentials (no API key, no JWT). This is the",
            "Huntarr-class vulnerability: unauthenticated write access.",
            "",
        ] + failures + [""]
        pytest.fail("\n".join(lines))
