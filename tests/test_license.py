"""
O.D.I.N. License Manager — Unit Tests

Tests Ed25519 license verification, tier resolution, expiry handling,
feature gating, and printer/user limit checks.

These are pure unit tests — no running server needed.
"""

import sys
import os
import json
import base64
import pytest
from datetime import date, timedelta
from unittest.mock import patch

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
)

import license_manager as lm


# ---------------------------------------------------------------------------
# Helpers — generate real Ed25519 keypairs for testing
# ---------------------------------------------------------------------------

def _generate_test_keypair():
    """Generate an Ed25519 keypair and return (private_key, public_key_pem)."""
    private_key = Ed25519PrivateKey.generate()
    pub_pem = private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return private_key, pub_pem


def _sign_license(private_key, payload: dict) -> str:
    """Create a signed license string: base64(payload).base64(signature)."""
    payload_bytes = json.dumps(payload).encode()
    signature = private_key.sign(payload_bytes)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode()
    sig_b64 = base64.urlsafe_b64encode(signature).decode()
    return f"{payload_b64}.{sig_b64}"


def _write_license(tmp_path, content: str) -> str:
    """Write a license file and return the path."""
    path = tmp_path / "odin.license"
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def keypair():
    """Fresh keypair for each test."""
    return _generate_test_keypair()


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the license cache between tests."""
    lm._cached_license = None
    lm._cached_mtime = 0
    yield
    lm._cached_license = None
    lm._cached_mtime = 0


# ---------------------------------------------------------------------------
# Tests: No license file (Community fallback)
# ---------------------------------------------------------------------------

class TestNoLicenseFile:
    def test_community_defaults(self):
        with patch.object(lm, "_find_license_file", return_value=None):
            info = lm.load_license()
        assert info.valid is False
        assert info.tier == "community"
        assert info.max_printers == 5
        assert info.max_users == 1
        assert info.features == []
        assert info.error is None

    def test_has_feature_community(self):
        with patch.object(lm, "_find_license_file", return_value=None):
            info = lm.load_license()
        # Community has no features
        assert info.has_feature("rbac") is False
        assert info.has_feature("orders") is False


# ---------------------------------------------------------------------------
# Tests: Invalid license formats
# ---------------------------------------------------------------------------

class TestInvalidLicense:
    def test_no_dot_separator(self, tmp_path):
        path = _write_license(tmp_path, "no-dot-separator-here")
        with patch.object(lm, "_find_license_file", return_value=path):
            info = lm.load_license()
        assert info.valid is False
        assert "Invalid license file format" in info.error

    def test_invalid_base64(self, tmp_path):
        path = _write_license(tmp_path, "not-valid-b64!!!.also-bad!!!")
        with patch.object(lm, "_find_license_file", return_value=path):
            info = lm.load_license()
        assert info.valid is False
        assert info.error is not None

    def test_invalid_signature(self, tmp_path, keypair):
        priv, pub_pem = keypair
        payload = {"tier": "pro", "licensee": "Test", "expires_at": "2099-12-31"}
        # Sign with correct key but verify with different key
        license_str = _sign_license(priv, payload)
        path = _write_license(tmp_path, license_str)

        # Use a DIFFERENT public key for verification
        _, other_pub = _generate_test_keypair()
        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", other_pub):
            info = lm.load_license()
        assert info.valid is False
        assert "Invalid license signature" in info.error

    def test_invalid_json_payload(self, tmp_path, keypair):
        priv, pub_pem = keypair
        # Sign garbage that isn't valid JSON
        garbage = b"this is not json"
        sig = priv.sign(garbage)
        payload_b64 = base64.urlsafe_b64encode(garbage).decode()
        sig_b64 = base64.urlsafe_b64encode(sig).decode()
        license_str = f"{payload_b64}.{sig_b64}"
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.valid is False
        assert info.error is not None


# ---------------------------------------------------------------------------
# Tests: Missing required fields
# ---------------------------------------------------------------------------

class TestMissingFields:
    @pytest.mark.parametrize("missing_field", ["tier", "licensee", "expires_at"])
    def test_missing_required_field(self, tmp_path, keypair, missing_field):
        priv, pub_pem = keypair
        payload = {"tier": "pro", "licensee": "Test", "expires_at": "2099-12-31"}
        del payload[missing_field]
        license_str = _sign_license(priv, payload)
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.valid is False
        assert f"missing required field: {missing_field}" in info.error.lower()


# ---------------------------------------------------------------------------
# Tests: Expired license
# ---------------------------------------------------------------------------

class TestExpiredLicense:
    def test_expired_falls_back_to_community(self, tmp_path, keypair):
        priv, pub_pem = keypair
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        payload = {"tier": "pro", "licensee": "Expired Corp", "expires_at": yesterday}
        license_str = _sign_license(priv, payload)
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.valid is False
        assert info.expired is True
        assert info.tier == "pro"  # Still shows original tier
        assert info.max_printers == 5  # Community limits
        assert info.max_users == 1
        assert info.features == []  # Community features


# ---------------------------------------------------------------------------
# Tests: Valid licenses
# ---------------------------------------------------------------------------

class TestValidLicense:
    def _make_valid(self, keypair, tier="pro", **extra):
        priv, pub_pem = keypair
        future = (date.today() + timedelta(days=365)).isoformat()
        payload = {
            "tier": tier,
            "licensee": "Test Corp",
            "email": "test@example.com",
            "issued_at": date.today().isoformat(),
            "expires_at": future,
            **extra,
        }
        return _sign_license(priv, payload), pub_pem

    def test_valid_pro(self, tmp_path, keypair):
        license_str, pub_pem = self._make_valid(keypair, "pro")
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.valid is True
        assert info.tier == "pro"
        assert info.licensee == "Test Corp"
        assert info.email == "test@example.com"
        assert info.expired is False
        # Pro features from TIERS definition
        assert "rbac" in info.features
        assert "orders" in info.features
        assert "webhooks" in info.features

    def test_valid_education(self, tmp_path, keypair):
        license_str, pub_pem = self._make_valid(keypair, "education")
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.valid is True
        assert info.tier == "education"
        assert "job_approval" in info.features
        assert "usage_reports" in info.features

    def test_valid_enterprise(self, tmp_path, keypair):
        license_str, pub_pem = self._make_valid(keypair, "enterprise")
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.valid is True
        assert info.tier == "enterprise"
        assert "audit_export" in info.features
        assert "opcua" in info.features

    def test_custom_limits_override_tier(self, tmp_path, keypair):
        license_str, pub_pem = self._make_valid(
            keypair, "pro", max_printers=10, max_users=3
        )
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.max_printers == 10
        assert info.max_users == 3

    def test_custom_features_override_tier(self, tmp_path, keypair):
        custom_features = ["rbac", "custom_thing"]
        license_str, pub_pem = self._make_valid(
            keypair, "pro", features=custom_features
        )
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.features == custom_features

    def test_has_feature_on_valid(self, tmp_path, keypair):
        license_str, pub_pem = self._make_valid(keypair, "pro")
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            info = lm.load_license()
        assert info.has_feature("rbac") is True
        assert info.has_feature("nonexistent") is False

    def test_to_dict(self, tmp_path, keypair):
        license_str, pub_pem = self._make_valid(keypair, "pro")
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem), \
             patch.object(lm, "get_installation_id", return_value="test-install-id"):
            info = lm.load_license()
            d = info.to_dict()
        assert d["valid"] is True
        assert d["tier"] == "pro"
        assert d["tier_name"] == "Pro"
        assert d["expired"] is False
        assert isinstance(d["features"], list)


# ---------------------------------------------------------------------------
# Tests: Gating functions (require_feature, check_printer_limit, etc.)
# ---------------------------------------------------------------------------

class TestGatingFunctions:
    def test_require_feature_raises_on_missing(self):
        from fastapi import HTTPException
        with patch.object(lm, "_find_license_file", return_value=None):
            lm._cached_license = None
            lm._cached_mtime = 0
            with pytest.raises(HTTPException) as exc_info:
                lm.require_feature("orders")
            assert exc_info.value.status_code == 403
            assert "orders" in str(exc_info.value.detail)

    def test_require_feature_passes_on_valid(self, tmp_path, keypair):
        priv, pub_pem = keypair
        future = (date.today() + timedelta(days=365)).isoformat()
        payload = {"tier": "pro", "licensee": "Test", "expires_at": future}
        license_str = _sign_license(priv, payload)
        path = _write_license(tmp_path, license_str)

        with patch.object(lm, "_find_license_file", return_value=path), \
             patch.object(lm, "ODIN_PUBLIC_KEY", pub_pem):
            lm._cached_license = None
            lm._cached_mtime = 0
            # Should not raise
            lm.require_feature("orders")

    def test_check_printer_limit_raises(self):
        from fastapi import HTTPException
        with patch.object(lm, "_find_license_file", return_value=None):
            lm._cached_license = None
            lm._cached_mtime = 0
            # Community allows 5 printers
            lm.check_printer_limit(4)  # Should pass (under limit)
            with pytest.raises(HTTPException) as exc_info:
                lm.check_printer_limit(5)  # At limit
            assert exc_info.value.status_code == 403
            assert "Printer limit" in str(exc_info.value.detail)

    def test_check_user_limit_raises(self):
        from fastapi import HTTPException
        with patch.object(lm, "_find_license_file", return_value=None):
            lm._cached_license = None
            lm._cached_mtime = 0
            # Community allows 1 user
            with pytest.raises(HTTPException) as exc_info:
                lm.check_user_limit(1)  # At limit
            assert exc_info.value.status_code == 403
            assert "User limit" in str(exc_info.value.detail)

    def test_check_user_limit_passes_under(self):
        from fastapi import HTTPException
        with patch.object(lm, "_find_license_file", return_value=None):
            lm._cached_license = None
            lm._cached_mtime = 0
            # 0 users < 1 max
            lm.check_user_limit(0)  # Should not raise
