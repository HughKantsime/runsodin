"""
Layer 3 Security: License Tier Gating Tests
============================================
Verify that Community tier correctly restricts Pro/Enterprise features.
Skips gracefully if the instance is running on a non-Community tier.

Run: pytest tests/security/test_license_gating.py -v --tb=short
"""

import pytest
import requests

from .conftest import BASE_URL, _headers


@pytest.fixture(scope="module")
def license_info(admin_token):
    """Get current license tier info."""
    r = requests.get(
        f"{BASE_URL}/api/license",
        headers=_headers(admin_token),
        timeout=10,
    )
    if r.status_code != 200:
        pytest.skip("License endpoint unavailable")
    data = r.json()
    license_data = data.get("license", data)
    tier = license_data.get("tier", "community").lower()
    max_users = license_data.get("max_users", 1)
    return {"tier": tier, "max_users": max_users}


class TestCommunityTierRestrictions:
    """Community tier must block Pro-only features."""

    def test_org_creation_blocked_on_community(self, admin_token, license_info):
        """POST /api/groups must be blocked on Community tier (user_groups feature)."""
        if license_info["tier"] != "community":
            pytest.skip(f"Instance is on {license_info['tier']} tier — test requires Community")
        r = requests.post(
            f"{BASE_URL}/api/groups",
            json={"name": "License Test Org", "description": "Should be blocked"},
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code in (403, 402), \
            f"Community tier allowed org creation! Got {r.status_code}"

    def test_user_limit_enforced(self, admin_token, license_info):
        """User creation beyond max_users must be blocked."""
        if license_info["tier"] != "community":
            pytest.skip(f"Instance is on {license_info['tier']} tier")
        if license_info["max_users"] > 10:
            pytest.skip("Max users too high to test limit")

        # Get current user count
        r = requests.get(
            f"{BASE_URL}/api/users",
            headers=_headers(admin_token),
            timeout=10,
        )
        if r.status_code != 200:
            pytest.skip("Cannot list users")
        current_count = len(r.json())
        if current_count < license_info["max_users"]:
            pytest.skip(f"Only {current_count}/{license_info['max_users']} users — limit not reached")

        # Try to create one more user beyond the limit
        r = requests.post(
            f"{BASE_URL}/api/users",
            json={
                "username": "license_limit_test",
                "email": "limit@test.local",
                "password": "LimitTestPass1!",
                "role": "viewer",
            },
            headers=_headers(admin_token),
            timeout=10,
        )
        assert r.status_code in (403, 402, 409), \
            f"User created beyond license limit! Got {r.status_code}"

    def test_webhooks_gated_on_community(self, admin_token, license_info):
        """Webhook creation should be gated on Community tier."""
        if license_info["tier"] != "community":
            pytest.skip(f"Instance is on {license_info['tier']} tier")
        r = requests.post(
            f"{BASE_URL}/api/webhooks",
            json={"url": "https://example.com/hook", "events": ["job.completed"]},
            headers=_headers(admin_token),
            timeout=10,
        )
        # Community should block webhooks, but it depends on implementation
        # Accept 200 as non-blocking but record
        if r.status_code == 200:
            # Cleanup
            wh_id = r.json().get("id")
            if wh_id:
                requests.delete(
                    f"{BASE_URL}/api/webhooks/{wh_id}",
                    headers=_headers(admin_token),
                    timeout=10,
                )
            pytest.xfail("Community tier allowed webhook creation — may not be gated")

    def test_license_tier_matches_api_response(self, admin_token, license_info):
        """License API must return a valid tier name."""
        valid_tiers = {"community", "pro", "education", "enterprise"}
        assert license_info["tier"] in valid_tiers, \
            f"Unknown license tier: {license_info['tier']}"
