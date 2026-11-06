"""Parsed license capabilities, using the canonical facade policy."""
from datetime import datetime, timezone
from typing import Optional, Dict, Any

class LicenseInfo:
    """Parsed and validated license information."""

    def __init__(self):
        self.valid = False
        self.tier = "community"
        self.key = ""
        self.licensee = ""
        self.email = ""
        self.issued_at = ""
        self.expires_at = ""
        self.max_printers = 5
        self.max_users = 1
        self.features = []
        self.error = None
        self.expired = False
        self.device_bound = False
        self.device_fingerprint = None
        self.binding_present = False
        self.binding_matches_current = False
        self.license_sha256 = ""
        self.expiry_deadline: Optional[datetime] = None  # set for valid licenses
        self.trial = False  # signed "trial": true (portal Pro trial), kept after expiry

    def days_left(self, now: Optional[datetime] = None) -> Optional[int]:
        """Whole days until a valid license expires (rounded up), None when not valid."""
        if not self.valid or self.expiry_deadline is None:
            return None
        remaining = (self.expiry_deadline - (now or datetime.now(timezone.utc))).total_seconds()
        if remaining <= 0:
            return 0
        return int(-(-remaining // 86400))

    def trial_days_left(self, now: Optional[datetime] = None) -> Optional[int]:
        """Whole days left on a trial (rounded up; 0 once ended), None if not a trial."""
        if not self.trial:
            return None
        days = self.days_left(now)
        return 0 if days is None else days

    def has_feature(self, feature: str) -> bool:
        """Check if the current license includes a feature.
        Checks both the license payload and the tier definition,
        so features added to a tier after license issuance are available."""
        from license_manager import TIERS, license_is_managed_externally, get_installation_id
        if not self.valid:
            return feature in TIERS["community"]["features"]
        tier_features = TIERS.get(self.tier, TIERS["community"])["features"]
        if feature == "training_signoff":
            return self.tier in {"education", "enterprise"} and self.has_feature("education_workflows")
        if feature == "education_workflows":
            return self.tier in {"education", "enterprise"} and feature in self.features
        return feature in self.features or feature in tier_features

    def effective_features(self) -> list[str]:
        """Return the deterministic feature set enforced by ``has_feature``."""
        from license_manager import TIERS, license_is_managed_externally, get_installation_id
        if not self.valid:
            return sorted(set(TIERS["community"]["features"]))
        tier_features = set(TIERS.get(self.tier, TIERS["community"])["features"])
        features = tier_features | set(self.features)
        if not self.has_feature("education_workflows"):
            features.discard("education_workflows")
            features.discard("training_signoff")
        return sorted(features)

    def to_dict(self) -> Dict[str, Any]:
        from license_manager import TIERS, license_is_managed_externally, get_installation_id
        tier_def = TIERS.get(self.tier, TIERS["community"])
        return {
            "valid": self.valid,
            "tier": self.tier,
            "tier_name": tier_def["name"],
            "key": self.key,
            "licensee": self.licensee,
            "email": self.email,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "expired": self.expired,
            "trial": self.trial,
            "trial_days_left": self.trial_days_left(),
            "max_printers": self.max_printers,
            "max_users": self.max_users,
            "features": self.effective_features(),
            "managed_externally": license_is_managed_externally(),
            "device_bound": self.device_bound,
            "binding_present": self.binding_present,
            "binding_matches_current": self.binding_matches_current,
            "license_sha256": self.license_sha256,
            "error": self.error,
            "device_fingerprint": self.device_fingerprint,
            "installation_id": get_installation_id(),
        }

    def to_public_dict(self) -> Dict[str, Any]:
        """Return license capabilities without customer or device identity.

        ``GET /api/license`` is intentionally unauthenticated so the login and
        setup screens can render tier-aware UI.  Keep that response limited to
        operational capability data; support diagnostics and activation flows
        use authenticated endpoints for installation identity.
        """
        from license_manager import TIERS, license_is_managed_externally, get_installation_id
        tier_def = TIERS.get(self.tier, TIERS["community"])
        return {
            "valid": self.valid,
            "tier": self.tier,
            "tier_name": tier_def["name"],
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "expired": self.expired,
            "trial": self.trial,
            "trial_days_left": self.trial_days_left(),
            "max_printers": self.max_printers,
            "max_users": self.max_users,
            "features": self.effective_features(),
            "managed_externally": license_is_managed_externally(),
            "device_bound": self.device_bound,
            "binding_present": self.binding_present,
            "binding_matches_current": self.binding_matches_current,
            "license_sha256": self.license_sha256,
        }

