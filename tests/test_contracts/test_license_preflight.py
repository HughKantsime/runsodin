"""Signed Education license preflight must enforce installation binding."""

import base64
import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import license_manager
from scripts.validate_license_file import validate_license_file


def _signed_license(tmp_path, installation_id=None):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        Encoding.PEM,
        PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    payload = {
        "tier": "education",
        "licensee": "Test School",
        "expires_at": (date.today() + timedelta(days=30)).isoformat(),
    }
    if installation_id is not None:
        payload["installation_id"] = installation_id
    payload_bytes = json.dumps(payload).encode()
    signature = private_key.sign(payload_bytes)
    content = ".".join((
        base64.urlsafe_b64encode(payload_bytes).decode(),
        base64.urlsafe_b64encode(signature).decode(),
    ))
    path = tmp_path / "odin.license"
    path.write_text(content)
    return path, public_key


def test_preflight_accepts_matching_installation_binding(tmp_path):
    path, public_key = _signed_license(tmp_path, "sandbox-install-id")
    with patch.object(license_manager, "ODIN_PUBLIC_KEY", public_key):
        payload = validate_license_file(
            path,
            expected_tier="education",
            expected_installation_id="sandbox-install-id",
        )
    assert payload["installation_id"] == "sandbox-install-id"


@pytest.mark.parametrize("license_installation_id", [None, "different-install-id"])
def test_preflight_rejects_missing_or_mismatched_binding(
    tmp_path,
    license_installation_id,
):
    path, public_key = _signed_license(tmp_path, license_installation_id)
    with patch.object(license_manager, "ODIN_PUBLIC_KEY", public_key), \
         pytest.raises(ValueError, match="installation"):
        validate_license_file(
            path,
            expected_tier="education",
            expected_installation_id="sandbox-install-id",
        )
