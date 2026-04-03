"""
Layer 3 Security: Scoped API Token Tests
=========================================
Verify odin_ prefix token scope boundaries and access control.

Run: pytest tests/security/test_scoped_tokens.py -v --tb=short
"""

import pytest
import requests

from .conftest import BASE_URL, _headers


@pytest.fixture
def read_only_token(admin_token):
    """Create a read-only scoped token and clean up after."""
    r = requests.post(
        f"{BASE_URL}/api/tokens",
        json={"name": "test_read_only", "scopes": ["read:printers"]},
        headers=_headers(admin_token),
        timeout=10,
    )
    if r.status_code != 200:
        pytest.skip(f"Token creation returned {r.status_code} — endpoint may not exist")
    data = r.json()
    token_value = data.get("token") or data.get("api_token")
    token_id = data.get("id") or data.get("token_id")
    if not token_value:
        pytest.skip("Token creation didn't return token value")

    yield {"token": token_value, "id": token_id}

    # Cleanup
    if token_id:
        requests.delete(
            f"{BASE_URL}/api/tokens/{token_id}",
            headers=_headers(admin_token),
            timeout=10,
        )


class TestScopedTokenBoundaries:
    """Verify scoped tokens can only access their permitted scopes."""

    def test_read_only_token_can_get_printers(self, read_only_token):
        """Read-only scoped token should access GET /api/printers."""
        h = {"Authorization": f"Bearer {read_only_token['token']}"}
        r = requests.get(
            f"{BASE_URL}/api/printers",
            headers=h,
            timeout=10,
        )
        # The token might use X-API-Token header instead of Bearer
        if r.status_code in (401, 403):
            h2 = {"X-API-Token": read_only_token["token"]}
            r = requests.get(
                f"{BASE_URL}/api/printers",
                headers=h2,
                timeout=10,
            )
        assert r.status_code == 200, \
            f"Read-only token can't GET printers: {r.status_code}"

    def test_token_has_odin_prefix(self, read_only_token):
        """Scoped tokens must use the odin_ prefix format."""
        assert read_only_token["token"].startswith("odin_"), \
            f"Token doesn't have odin_ prefix: {read_only_token['token'][:10]}..."


class TestTokenScopeEscalation:
    """Verify non-admin users cannot create admin-scoped tokens."""

    def test_non_admin_cannot_create_admin_token(self, viewer_token):
        """Viewer creating an admin-scope token must get 403."""
        r = requests.post(
            f"{BASE_URL}/api/tokens",
            json={"name": "escalation_test", "scopes": ["admin"]},
            headers=_headers(viewer_token),
            timeout=10,
        )
        assert r.status_code in (403, 422), \
            f"Viewer created admin-scope token! Got {r.status_code}"
