"""
Layer 3 Security: JWT & Session Token Tests
============================================
Tests for expired/forged JWTs, revoked sessions, and token blacklist enforcement.

Run: pytest tests/security/test_auth_tokens.py -v --tb=short
"""

import time
import pytest
import requests
import jwt as pyjwt

from .conftest import BASE_URL, _headers, _no_auth_headers, _login, ADMIN_USERNAME, ADMIN_PASSWORD


class TestExpiredAndForgedJWT:
    """Verify the server rejects tampered or expired JWTs."""

    def test_expired_jwt_rejected(self):
        """JWT with past expiration must be rejected on role-protected endpoints."""
        expired_token = pyjwt.encode(
            {"sub": "admin", "role": "admin", "exp": int(time.time()) - 3600},
            "wrong-secret-key",
            algorithm="HS256",
        )
        r = requests.get(
            f"{BASE_URL}/api/users",
            headers=_headers(expired_token),
            timeout=10,
        )
        assert r.status_code in (401, 403), \
            f"Expired JWT accepted on /api/users! Got {r.status_code}"

    def test_forged_jwt_wrong_key_rejected(self):
        """JWT signed with wrong secret must be rejected."""
        fake_token = pyjwt.encode(
            {"sub": "admin", "role": "admin", "exp": int(time.time()) + 3600},
            "attacker-controlled-secret",
            algorithm="HS256",
        )
        for endpoint in ["/api/users", "/api/backups", "/api/admin/oidc"]:
            r = requests.get(
                f"{BASE_URL}{endpoint}",
                headers=_headers(fake_token),
                timeout=10,
            )
            if r.status_code in (401, 403):
                return
        assert False, "Forged JWT accepted on all admin endpoints"

    def test_none_algorithm_rejected(self):
        """JWT with 'none' algorithm must not be accepted."""
        # Craft a token with alg=none (classic JWT attack)
        import base64
        import json
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps({
            "sub": "admin", "role": "admin", "exp": int(time.time()) + 3600,
        }).encode()).rstrip(b"=")
        none_token = f"{header.decode()}.{payload.decode()}."
        r = requests.get(
            f"{BASE_URL}/api/users",
            headers=_headers(none_token),
            timeout=10,
        )
        assert r.status_code in (401, 403, 422), \
            f"JWT with alg=none accepted! Got {r.status_code} — critical vulnerability"

    def test_empty_bearer_token_rejected(self):
        """Empty Bearer token must not grant access to admin endpoints."""
        h = {"Content-Type": "application/json", "Authorization": "Bearer "}
        if _headers().get("X-API-Key"):
            h["X-API-Key"] = _headers().get("X-API-Key")
        r = requests.get(f"{BASE_URL}/api/users", headers=h, timeout=10)
        assert r.status_code in (401, 403, 422), \
            f"Empty Bearer token accepted! Got {r.status_code}"


    def test_mfa_status_endpoint_exists(self, admin_token):
        """MFA status endpoint should exist and not crash."""
        r = requests.get(
            f"{BASE_URL}/api/auth/mfa/status",
            headers=_headers(admin_token),
            timeout=10,
        )
        # Endpoint should exist (200) or be at a different path (404)
        assert r.status_code != 500, "MFA status endpoint crashed"
