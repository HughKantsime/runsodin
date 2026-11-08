"""Frozen Slice A verifier from 412e30a8 (2.0.47 + slice A).

Only the historical algorithm is copied; tests inject signing keys and clocks.
Do not update this oracle to match the implementation under test.
"""
import base64
import hashlib
import json
from license_manager import (LicenseInfo, TIERS, TRIAL_DEVICE_MISMATCH,
    _read_existing_installation_id, _read_existing_device_pubkey,
    _verify_signature, license_binding_is_required, _license_expiry_deadline, license_clock)

def verify_license_text(raw: str, installation_id_reader=None, device_pubkey_reader=None) -> LicenseInfo:
    """Verify candidate bytes using runtime rules, without installing a file."""
    info = LicenseInfo()
    installation_id_reader = installation_id_reader or _read_existing_installation_id
    device_pubkey_reader = device_pubkey_reader or _read_existing_device_pubkey
    try:
        # License format: two base64 blocks separated by a dot
        # <base64_payload>.<base64_signature>
        parts = raw.split(".")
        if len(parts) != 2:
            info.error = "Invalid license file format"
            return info

        payload_b64, sig_b64 = parts
        # base64url from Node strips padding — re-add for Python
        payload_b64_padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        sig_b64_padded = sig_b64 + "=" * (-len(sig_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
        sig_bytes = base64.urlsafe_b64decode(sig_b64_padded)

        # Verify signature
        if not _verify_signature(payload_bytes, sig_bytes):
            info.error = "Invalid license signature"
            return info

        # The digest becomes evidence only after signature verification. This
        # lets an external controller compare the exact mounted artifact with
        # application-observed state without exposing the artifact itself.
        info.license_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        # Parse payload
        payload = json.loads(payload_bytes.decode("utf-8"))

        # Check required fields
        for field in ["tier", "licensee", "expires_at"]:
            if field not in payload:
                info.error = f"License missing required field: {field}"
                return info

        # Only a signed boolean true marks a trial (the signature covers it).
        # Reported for valid and expired licenses, not for refused ones.
        is_trial = payload.get("trial") is True

        payload_install_id = payload.get("installation_id")
        info.binding_present = bool(payload_install_id)
        if payload_install_id:
            info.binding_matches_current = payload_install_id == installation_id_reader()
            if not info.binding_matches_current:
                info.error = "License is bound to a different installation"
                return info
        elif license_binding_is_required():
            info.error = "License must be bound to this installation"
            return info

        binding_fields = {"binding_version", "device_pubkey", "device_fingerprint"}
        # Trials predate the paid protocol and keep their existing key checks.
        if not is_trial and binding_fields.intersection(payload):
            info.device_bound = True
            pubkey = payload.get("device_pubkey")
            fingerprint = payload.get("device_fingerprint")
            try:
                raw_key = base64.b64decode(pubkey, validate=True) if isinstance(pubkey, str) else b""
                well_formed = (
                    type(payload.get("binding_version")) is int and payload["binding_version"] == 1
                    and isinstance(payload_install_id, str) and bool(payload_install_id)
                    and len(raw_key) == 32 and base64.b64encode(raw_key).decode("ascii") == pubkey
                    and fingerprint == hashlib.sha256(raw_key).hexdigest()
                )
            except (ValueError, TypeError):
                well_formed = False
            if not well_formed:
                info.error = "Unsupported or malformed device binding. Request a new device-v1 license from the portal."
                return info
            info.device_fingerprint = fingerprint
            if pubkey != device_pubkey_reader():
                info.error = "Device key mismatch. Restore this installation's original .odin-device.key and installation ID, or contact support for an authorized re-issue."
                return info
        trial_device = payload.get("device_pubkey") if is_trial else None
        if trial_device is not None and trial_device != device_pubkey_reader():
            info.error = TRIAL_DEVICE_MISMATCH
            return info

        # Check expiry
        expires = _license_expiry_deadline(payload["expires_at"])
        now, rollback = license_clock().sample()
        clock_error = license_clock().error(payload.get("issued_at", ""), sample=(now, rollback))
        if clock_error or now >= expires:
            info.valid = False
            info.expired = True
            info.error = clock_error or f"License expired on {payload['expires_at']}"
            # Still set tier info so UI can show "expired Pro" vs "no license"
            info.tier = payload["tier"]
            info.licensee = payload.get("licensee", "")
            info.expires_at = payload["expires_at"]
            info.trial = is_trial
            # Fall back to community features
            info.max_printers = TIERS["community"]["max_printers"]
            info.max_users = TIERS["community"]["max_users"]
            info.features = TIERS["community"]["features"]
            return info

        # The license portal signs "features": null when the admin leaves the
        # feature list blank; null means "tier default", exactly like an
        # absent field. Any other non-list value is malformed.
        payload_features = payload.get("features")
        if payload_features is not None and not (
            isinstance(payload_features, list)
            and all(isinstance(feature, str) for feature in payload_features)
        ):
            info.error = "License contains an invalid feature list"
            return info

        # Valid license
        tier = payload["tier"]
        tier_def = TIERS.get(tier, TIERS["community"])

        info.valid = True
        info.tier = tier
        info.key = payload.get("key", "")
        info.licensee = payload.get("licensee", "")
        info.email = payload.get("email", "")
        info.issued_at = payload.get("issued_at", "")
        info.expires_at = payload["expires_at"]
        info.expiry_deadline = expires
        info.trial = is_trial
        max_printers = payload.get("max_printers")
        info.max_printers = tier_def["max_printers"] if max_printers is None else max_printers
        # The activation service uses null for an unspecified tier-default cap.
        # Preserve explicit numeric limits, including zero.
        max_users = payload.get("max_users")
        info.max_users = tier_def["max_users"] if max_users is None else max_users
        if payload_features is not None:
            info.features = payload_features
        else:
            # null (the portal's blank feature list) means the tier's default
            # features, education_workflows included for Education/Enterprise.
            # An explicit list must still name education_workflows.
            info.features = list(tier_def["features"])
        info.expired = False

        return info

    except json.JSONDecodeError:
        info.error = "License file contains invalid data"
        return info
    except Exception:
        info.error = "License file contains invalid data"
        return info

