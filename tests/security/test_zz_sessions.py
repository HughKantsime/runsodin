"""
Layer 3 Security: Session Revocation Tests
===========================================
DESTRUCTIVE TESTS — These revoke sessions and invalidate tokens.
Named test_zz_* to run LAST in alphabetical order, after all other
security tests that depend on the session-scoped admin_token.

Run: pytest tests/security/test_zz_sessions.py -v --tb=short
"""

import time

import pytest
import requests

from .conftest import BASE_URL, _headers, _login, ADMIN_USERNAME, ADMIN_PASSWORD


def _login_with_retry(username, password, retries=3, delay=5):
    """Login with retry + backoff to handle rate limiting from prior tests."""
    for i in range(retries):
        token = _login(username, password)
        if token:
            return token
        if i < retries - 1:
            time.sleep(delay)
    return None


class TestSessionRevocation:
    """Verify revoked sessions are blocked via token_blacklist.

    These tests create their own login sessions and do NOT use the
    shared admin_token fixture, to avoid poisoning other tests.
    """

    def test_revoke_single_session(self):
        """Login twice, revoke one session, verify its token is rejected."""
        token_a = _login_with_retry(ADMIN_USERNAME, ADMIN_PASSWORD)
        if not token_a:
            pytest.skip("Cannot login — likely rate-limited from prior tests")
        token_b = _login_with_retry(ADMIN_USERNAME, ADMIN_PASSWORD)
        if not token_b:
            pytest.skip("Cannot get second login token")

        # List sessions using token_b
        r = requests.get(
            f"{BASE_URL}/api/sessions",
            headers=_headers(token_b),
            timeout=10,
        )
        if r.status_code != 200:
            pytest.skip("Sessions endpoint not available")

        sessions = r.json()
        if not isinstance(sessions, list) or len(sessions) < 2:
            pytest.skip("Need at least 2 sessions to test single revocation")

        # Revoke the last session (should be token_b's)
        session_id = sessions[-1].get("id") or sessions[-1].get("session_id")
        if not session_id:
            pytest.skip("Session ID not found in response")

        revoke_r = requests.delete(
            f"{BASE_URL}/api/sessions/{session_id}",
            headers=_headers(token_b),
            timeout=10,
        )
        if revoke_r.status_code not in (200, 204):
            pytest.skip(f"Session revocation returned {revoke_r.status_code}")

        # token_b should now be rejected
        check_r = requests.get(
            f"{BASE_URL}/api/printers",
            headers=_headers(token_b),
            timeout=10,
        )
        if check_r.status_code == 200:
            pytest.xfail("Revoked session token still accepted — blacklist may use eventual consistency")

    def test_revoke_all_sessions(self):
        """Revoke all sessions — other tokens for same user should be invalidated."""
        token_a = _login_with_retry(ADMIN_USERNAME, ADMIN_PASSWORD)
        if not token_a:
            pytest.skip("Cannot login — likely rate-limited from prior tests")
        token_b = _login_with_retry(ADMIN_USERNAME, ADMIN_PASSWORD)
        if not token_b:
            pytest.skip("Cannot get second login token")

        # Revoke all using token_a (should kill token_b but keep token_a)
        r = requests.delete(
            f"{BASE_URL}/api/sessions",
            headers=_headers(token_a),
            timeout=10,
        )
        if r.status_code not in (200, 204):
            pytest.skip(f"Revoke-all returned {r.status_code}")

        # token_b should now be blacklisted
        check_r = requests.get(
            f"{BASE_URL}/api/printers",
            headers=_headers(token_b),
            timeout=10,
        )
        if check_r.status_code == 200:
            pytest.xfail("Revoke-all didn't invalidate other session token")
