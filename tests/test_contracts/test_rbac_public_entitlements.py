"""Public SSO gates must not relax authentication expectations on other routes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("tier", ["community", "pro", "education", "enterprise"])
def test_public_sso_expectations_preserve_the_rest_of_the_matrix(tier):
    # The live matrix imports tests/conftest.py. Isolate it from the contract
    # conftest module and select the tier before its parameters are generated.
    code = '''
import test_rbac as matrix

public_sso = {("GET", "/api/auth/oidc/login"), ("POST", "/api/auth/oidc/exchange")}
assert set(matrix.PUBLIC_LICENSE_GATED) == public_sso
seen = set()
for method, path, original, body, notes in matrix.ENDPOINT_MATRIX:
    route = (method, path)
    actual = matrix._expectations(method, path, original)
    if route in public_sso:
        seen.add(route)
        assert matrix.LICENSE_GATED[route] == "sso"
        assert actual == {**original, "no_headers": 403 if matrix.LICENSE_TIER == "community" else 200}
    else:
        assert actual["no_headers"] == original["no_headers"], route
assert seen == public_sso
'''
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "tests"), "RBAC_LICENSE_TIER": tier},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
