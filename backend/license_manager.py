"""
O.D.I.N. License Manager

Air-gap friendly license validation using Ed25519 signatures.
No phone home, no cloud validation. License file is verified
locally against the embedded public key.

License file format: base64-encoded JSON payload + signature
"""

import json
import base64
import os
from datetime import datetime, date
from typing import Optional, Dict, Any
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


# ── Embedded public key (ships with the software) ──
# Replace this with your actual public key after running generate_license.py --keygen
ODIN_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEASQ/2C3Ee4lswyEhLoQTtualXqnKhSngyPwoF8mxg/L0=
-----END PUBLIC KEY-----
"""

# ── Tier definitions ──
TIERS = {
    "community": {
        "name": "Community",
        "max_printers": 5,
        "max_users": 1,
        "features": [],
    },
    "pro": {
        "name": "Pro",
        "max_printers": 9999,
        "max_users": 9999,
        "features": [
            "multi_user", "rbac", "sso", "white_label",
            "webhooks", "email_notifications", "push_notifications",
            "orders", "products", "bom",
            "analytics", "csv_export", "cost_calculator",
            "maintenance", "care_counters",
        ],
    },
    "education": {
        "name": "Education",
        "max_printers": 9999,
        "max_users": 9999,
        "features": [
            "multi_user", "rbac", "sso", "white_label",
            "webhooks", "email_notifications", "push_notifications",
            "orders", "products", "bom",
            "analytics", "csv_export", "cost_calculator",
            "maintenance", "care_counters",
            "job_approval", "usage_reports",
        ],
    },
    "enterprise": {
        "name": "Enterprise",
        "max_printers": 9999,
        "max_users": 9999,
        "features": [
            "multi_user", "rbac", "sso", "white_label",
            "webhooks", "email_notifications", "push_notifications",
            "orders", "products", "bom",
            "analytics", "csv_export", "cost_calculator",
            "maintenance", "care_counters",
            "job_approval", "usage_reports",
            "opcua", "mqtt_republish", "audit_export", "sqlcipher",
        ],
    },
}

# Where license files are stored
LICENSE_DIR = os.environ.get("LICENSE_DIR", "/data")
LICENSE_FILENAME = "odin.license"


class LicenseInfo:
    """Parsed and validated license information."""

    def __init__(self):
        self.valid = False
        self.tier = "community"
        self.licensee = ""
        self.email = ""
        self.issued_at = ""
        self.expires_at = ""
        self.max_printers = 5
        self.max_users = 1
        self.features = []
        self.error = None
        self.expired = False

    def has_feature(self, feature: str) -> bool:
        """Check if the current license includes a feature."""
        if not self.valid:
            return feature in TIERS["community"]["features"]
        return feature in self.features

    def to_dict(self) -> Dict[str, Any]:
        tier_def = TIERS.get(self.tier, TIERS["community"])
        return {
            "valid": self.valid,
            "tier": self.tier,
            "tier_name": tier_def["name"],
            "licensee": self.licensee,
            "email": self.email,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "expired": self.expired,
            "max_printers": self.max_printers,
            "max_users": self.max_users,
            "features": self.features,
            "error": self.error,
        }


def _find_license_file() -> Optional[str]:
    """Look for a license file in known locations."""
    search_paths = [
        os.path.join(LICENSE_DIR, LICENSE_FILENAME),
        os.path.join(os.path.dirname(__file__), LICENSE_FILENAME),
        os.path.join(os.path.dirname(__file__), "..", LICENSE_FILENAME),
        os.path.join("/data", LICENSE_FILENAME),
    ]
    for path in search_paths:
        if os.path.isfile(path):
            return path
    return None


def _verify_signature(payload_bytes: bytes, signature_bytes: bytes) -> bool:
    """Verify Ed25519 signature against embedded public key."""
    if not CRYPTO_AVAILABLE:
        return False
    try:
        public_key = load_pem_public_key(ODIN_PUBLIC_KEY.encode())
        public_key.verify(signature_bytes, payload_bytes)
        return True
    except Exception:
        return False


def load_license() -> LicenseInfo:
    """Load and validate the license file. Returns LicenseInfo."""
    info = LicenseInfo()

    license_path = _find_license_file()
    if not license_path:
        # No license file = Community tier (valid, just limited)
        info.valid = False
        info.tier = "community"
        info.max_printers = TIERS["community"]["max_printers"]
        info.max_users = TIERS["community"]["max_users"]
        info.features = TIERS["community"]["features"]
        return info

    try:
        with open(license_path, "r") as f:
            raw = f.read().strip()

        # License format: two base64 blocks separated by a dot
        # <base64_payload>.<base64_signature>
        parts = raw.split(".")
        if len(parts) != 2:
            info.error = "Invalid license file format"
            return info

        payload_b64, sig_b64 = parts
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        sig_bytes = base64.urlsafe_b64decode(sig_b64)

        # Verify signature
        if not _verify_signature(payload_bytes, sig_bytes):
            info.error = "Invalid license signature"
            return info

        # Parse payload
        payload = json.loads(payload_bytes.decode("utf-8"))

        # Check required fields
        for field in ["tier", "licensee", "expires_at"]:
            if field not in payload:
                info.error = f"License missing required field: {field}"
                return info

        # Check expiry
        expires_str = payload["expires_at"].split("T")[0]  # Handle both "2027-02-09" and "2027-02-09T23:59:59Z"
        expires = datetime.strptime(expires_str, "%Y-%m-%d").date()
        if expires < date.today():
            info.valid = False
            info.expired = True
            info.error = f"License expired on {payload['expires_at']}"
            # Still set tier info so UI can show "expired Pro" vs "no license"
            info.tier = payload["tier"]
            info.licensee = payload.get("licensee", "")
            info.expires_at = payload["expires_at"]
            # Fall back to community features
            info.max_printers = TIERS["community"]["max_printers"]
            info.max_users = TIERS["community"]["max_users"]
            info.features = TIERS["community"]["features"]
            return info

        # Valid license
        tier = payload["tier"]
        tier_def = TIERS.get(tier, TIERS["community"])

        info.valid = True
        info.tier = tier
        info.licensee = payload.get("licensee", "")
        info.email = payload.get("email", "")
        info.issued_at = payload.get("issued_at", "")
        info.expires_at = payload["expires_at"]
        info.max_printers = payload.get("max_printers", tier_def["max_printers"]) or tier_def["max_printers"]
        info.max_users = payload.get("max_users", tier_def["max_users"])
        info.features = payload.get("features", tier_def["features"])
        info.expired = False

        return info

    except json.JSONDecodeError:
        info.error = "License file contains invalid data"
        return info
    except Exception as e:
        info.error = f"Failed to read license: {str(e)}"
        return info


def save_license_file(content: str, directory: str = None) -> str:
    """Save a license file to the data directory. Returns the path."""
    target_dir = directory or LICENSE_DIR
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, LICENSE_FILENAME)
    with open(path, "w") as f:
        f.write(content.strip())
    return path


# ── Cached license (reloaded when file changes) ──
_cached_license: Optional[LicenseInfo] = None
_cached_mtime: float = 0


def get_license() -> LicenseInfo:
    """Get the current license, with caching. Reloads if file changed."""
    global _cached_license, _cached_mtime

    license_path = _find_license_file()
    current_mtime = os.path.getmtime(license_path) if license_path else 0

    if _cached_license is None or current_mtime != _cached_mtime:
        _cached_license = load_license()
        _cached_mtime = current_mtime

    return _cached_license


def require_feature(feature: str):
    """Check if a feature is available. Raises if not."""
    from fastapi import HTTPException
    license_info = get_license()
    if not license_info.has_feature(feature):
        tier = license_info.tier
        raise HTTPException(
            status_code=403,
            detail=f"Feature '{feature}' requires a Pro or higher license. Current tier: {tier}"
        )


def check_printer_limit(current_count: int):
    """Check if adding another printer would exceed the license limit."""
    from fastapi import HTTPException
    license_info = get_license()
    if current_count >= license_info.max_printers:
        raise HTTPException(
            status_code=403,
            detail=f"Printer limit reached ({license_info.max_printers}). Upgrade to Pro for unlimited printers."
        )


def check_user_limit(current_count: int):
    """Check if adding another user would exceed the license limit."""
    from fastapi import HTTPException
    license_info = get_license()
    if current_count >= license_info.max_users:
        raise HTTPException(
            status_code=403,
            detail=f"User limit reached ({license_info.max_users}). Upgrade to Pro for unlimited users."
        )
