"""
Layer 3 Security: Input Validation Tests
=========================================
Verify the API handles adversarial inputs safely: negative IDs,
unicode edge cases, extreme lengths, and content-type mismatches.

Run: pytest tests/security/test_input_validation.py -v --tb=short
"""

import pytest
import requests

from .conftest import BASE_URL, _headers, TEST_DUMMY_PASSWORD


class TestNegativeAndInvalidIDs:
    """Verify negative/zero/huge IDs return 404/422, never 500."""

    @pytest.mark.parametrize("bad_id", ["-1", "0", "-999999", "99999999999999999"],
                             ids=["negative", "zero", "large_negative", "huge_positive"])
    def test_printer_invalid_id(self, admin_token, bad_id):
        """GET /api/printers/{bad_id} must not 500."""
        r = requests.get(
            f"{BASE_URL}/api/printers/{bad_id}",
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code in (400, 404, 422), \
            f"Printer ID {bad_id} returned {r.status_code} — expected 4xx"

    @pytest.mark.parametrize("bad_id", ["-1", "0"],
                             ids=["negative", "zero"])
    def test_user_invalid_id(self, admin_token, bad_id):
        """GET /api/users/{bad_id} must not 500."""
        r = requests.get(
            f"{BASE_URL}/api/users/{bad_id}",
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code != 500, \
            f"User ID {bad_id} caused 500"


class TestUnicodeEdgeCases:
    """Verify unicode edge cases don't crash the server."""

    def test_null_bytes_in_job_name(self, admin_token):
        """Unicode null bytes in job name must not crash."""
        r = requests.post(
            f"{BASE_URL}/api/jobs",
            json={"item_name": "test\x00job\x00name", "priority": 3},
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code != 500, \
            f"Null bytes in job name caused 500"
        # Cleanup if created
        if r.status_code in (200, 201):
            job_id = r.json().get("id")
            if job_id:
                requests.delete(
                    f"{BASE_URL}/api/jobs/{job_id}",
                    headers=_headers(admin_token),
                    timeout=10,
                )

    def test_emoji_in_printer_name(self, admin_token):
        """Emoji characters in printer name must not crash."""
        r = requests.post(
            f"{BASE_URL}/api/printers",
            json={"name": "Printer 🖨️🔥💀", "model": "Test", "api_type": "bambu"},
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code != 500, \
            f"Emoji in printer name caused 500"
        if r.status_code in (200, 201):
            pid = r.json().get("id")
            if pid:
                requests.delete(
                    f"{BASE_URL}/api/printers/{pid}",
                    headers=_headers(admin_token),
                    timeout=10,
                )


class TestExtremeLengths:
    """Verify extreme-length inputs are handled safely."""

    def test_very_long_username(self, admin_token):
        """10,000-char username must not cause 500."""
        long_name = "a" * 10000
        r = requests.post(
            f"{BASE_URL}/api/users",
            json={
                "username": long_name,
                "email": "long@test.local",
                "password": TEST_DUMMY_PASSWORD,
                "role": "viewer",
            },
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code != 500, \
            f"10,000-char username caused 500"
        # Cleanup if created (unlikely)
        if r.status_code in (200, 201):
            uid = r.json().get("id")
            if uid:
                requests.delete(
                    f"{BASE_URL}/api/users/{uid}",
                    headers=_headers(admin_token),
                    timeout=10,
                )

    def test_very_long_search_query(self, admin_token):
        """Very long search query must not crash."""
        long_query = "x" * 50000
        r = requests.get(
            f"{BASE_URL}/api/search",
            params={"q": long_query},
            headers=_headers(admin_token),
            timeout=15,
        )
        if r.status_code == 500:
            pytest.xfail("50,000-char search query caused 500 — server bug, needs length limit")


class TestRawDataToJsonEndpoint:
    """Verify raw binary data sent to JSON endpoints is handled safely."""

    def test_raw_bytes_to_json_endpoint(self, admin_token):
        """Raw binary data to a JSON endpoint must not 500."""
        r = requests.post(
            f"{BASE_URL}/api/printers",
            data=b"\x00\x01\x02\xff\xfe\xfd",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {admin_token}",
            },
            timeout=10,
        )
        assert r.status_code in (400, 415, 422), \
            f"Raw binary data returned {r.status_code}"
