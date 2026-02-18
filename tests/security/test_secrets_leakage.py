"""
Layer 3 Security: Secrets Leakage Tests
========================================
Verify error responses don't leak secrets, stack traces, or internal details.
Verify encrypted-at-rest data is not returned in plaintext.

Run: pytest tests/security/test_secrets_leakage.py -v --tb=short
"""

import pytest
import requests

from .conftest import BASE_URL, _headers, _no_auth_headers, API_KEY


# Patterns that should never appear in API responses
LEAK_PATTERNS = [
    "Traceback (most recent call last)",
    "sqlalchemy.exc.",
    "sqlite3.OperationalError",
    "password_hash",
    "ENCRYPTION_KEY",
    "JWT_SECRET_KEY",
    "Fernet",
    "File \"/app/",
    "File \"/backend/",
]


class TestErrorResponseLeakage:
    """Verify error responses don't leak implementation details."""

    def test_404_no_traceback(self, admin_token):
        """404 response must not contain Python traceback."""
        r = requests.get(
            f"{BASE_URL}/api/nonexistent-endpoint-xyz",
            headers=_headers(admin_token),
            timeout=10,
        )
        body = r.text
        for pattern in LEAK_PATTERNS:
            assert pattern not in body, \
                f"404 response leaks '{pattern}'"

    def test_malformed_post_no_stack_trace(self, admin_token):
        """Malformed POST body must not leak stack trace."""
        r = requests.post(
            f"{BASE_URL}/api/printers",
            data="this is not json {{{{",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_token}",
            },
            timeout=10,
        )
        body = r.text
        for pattern in LEAK_PATTERNS:
            assert pattern not in body, \
                f"Malformed POST leaks '{pattern}'"
        assert r.status_code != 500, "Malformed POST caused 500"

    def test_invalid_id_no_stack_trace(self, admin_token):
        """Non-numeric ID must not cause 500 with stack trace."""
        r = requests.get(
            f"{BASE_URL}/api/printers/not-a-number",
            headers=_headers(admin_token),
            timeout=10,
        )
        body = r.text
        for pattern in LEAK_PATTERNS:
            assert pattern not in body, \
                f"Invalid ID leaks '{pattern}'"
        assert r.status_code in (400, 404, 422), \
            f"Non-numeric ID returned {r.status_code}"


class TestUserEnumerationPrevention:
    """Verify login errors don't reveal whether a user exists."""

    def test_nonexistent_user_same_status_as_wrong_password(self):
        """Login with nonexistent user vs wrong password must return same status."""
        h = {}
        if API_KEY:
            h["X-API-Key"] = API_KEY

        # Nonexistent user
        r1 = requests.post(
            f"{BASE_URL}/api/auth/login",
            data={"username": "definitely_not_a_real_user_xyz", "password": "SomePass1!"},
            headers=h,
            timeout=10,
        )

        # Real admin user with wrong password
        r2 = requests.post(
            f"{BASE_URL}/api/auth/login",
            data={"username": "admin", "password": "DefinitelyWrongPassword1!"},
            headers=h,
            timeout=10,
        )

        # Both should return the same status (typically 401)
        # Allow 429 if rate-limited
        if r1.status_code == 429 or r2.status_code == 429:
            pytest.skip("Rate-limited during enumeration test")
        assert r1.status_code == r2.status_code, \
            f"User enumeration: nonexistent={r1.status_code}, wrong_pass={r2.status_code}"


class TestEncryptedAtRest:
    """Verify secrets are not returned in plaintext via API."""

    def test_printer_list_no_plaintext_api_keys(self, admin_token):
        """GET /api/printers must not return plaintext API keys."""
        r = requests.get(
            f"{BASE_URL}/api/printers",
            headers=_headers(admin_token),
            timeout=10,
        )
        if r.status_code != 200:
            pytest.skip("No printers endpoint")
        printers = r.json()
        if not isinstance(printers, list):
            printers = printers.get("printers", [])
        for p in printers:
            api_key = p.get("api_key", "")
            if api_key:
                # Should be masked, encrypted, or absent — not a raw credential
                # Bambu format: serial|access_code — if we see "|", it's plaintext
                assert "|" not in api_key, \
                    f"Printer {p.get('name')} has plaintext api_key containing '|'"
                # Should not look like a raw access code (8 alphanumeric chars)
                if len(api_key) == 8 and api_key.isalnum():
                    pytest.fail(f"Printer {p.get('name')} may have plaintext access code")

    def test_oidc_config_no_client_secret(self, admin_token):
        """GET /api/admin/oidc must not expose client_secret."""
        r = requests.get(
            f"{BASE_URL}/api/admin/oidc",
            headers=_headers(admin_token),
            timeout=10,
        )
        if r.status_code != 200:
            pytest.skip(f"OIDC endpoint returned {r.status_code}")
        data = r.json()
        # Should have has_client_secret (bool) not client_secret (string)
        assert "client_secret" not in data, \
            "OIDC config exposes client_secret in response"
        assert "client_secret_encrypted" not in data, \
            "OIDC config exposes encrypted client_secret in response"
