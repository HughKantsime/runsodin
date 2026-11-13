"""
O.D.I.N. License Manager

Air-gap friendly license validation using Ed25519 signatures.
No phone home, no cloud validation. License file is verified
locally against the embedded public key.

License file format: base64-encoded JSON payload + signature
"""

import json
import base64
import hashlib
import os
import re
import uuid
import tempfile
import time
import threading
import fcntl
import stat as stat_module
from contextlib import contextmanager
from datetime import datetime, date, time as datetime_time, timezone
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
MCowBQYDK2VwAyEA7PGXt2pCPkWUtPXbjwIjRTGoRwsYdmEzsSsS2AGAy28=
-----END PUBLIC KEY-----
"""

from core.license_tiers import TIERS

# Pro trial (spec .claude/specs/pro-trial.md): the portal signs "trial": true
# into a 14-day Pro license. Admins get a one-time notice this many days out.
TRIAL_NOTICE_DAYS = 3

# Where license files are stored
LICENSE_DIR = os.environ.get("LICENSE_DIR", "/data")
LICENSE_FILENAME = "odin.license"
INSTALL_ID_FILENAME = ".odin-install-id"
DEVICE_PRIVATE_KEY_FILENAME = ".odin-device.key"


def get_installation_id() -> str:
    from license_device import get_installation_id as read
    return read()


def get_device_keypair():
    from core.license_device_key import get_device_keypair as read_key
    return read_key(LICENSE_DIR, DEVICE_PRIVATE_KEY_FILENAME, CRYPTO_AVAILABLE)

def sign_license_challenge(action: str, key: str, installation_id: str, nonce: str) -> str:
    from license_device import sign_license_challenge as sign
    return sign(action, key, installation_id, nonce)


# ── Pro trial request (signed with the device key) ──
# The trial starts inside ODIN: the request is signed with the same device key
# that activation binds, so the portal can key one-trial-per-installation on
# something a stranger cannot claim. Canonical message (api/_lib/trial.js
# trialRequestMessage on the portal must match):
#   odin-trial-request-v1\n{installation_id}\n{device_pubkey}\n{email}\n{nonce}\n{issued_at}
TRIAL_REQUEST_VERSION = 1
TRIAL_REQUEST_CODE_PREFIX = "ODINTRIAL1."
TRIAL_REQUEST_MAX_AGE_MINUTES = 15
_EMAIL_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_EMAIL_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_TRIAL_EMAIL_RE = re.compile(rf"^{_EMAIL_LOCAL}@{_EMAIL_LABEL}(?:\.{_EMAIL_LABEL})+$")


def validate_trial_email(email: str) -> str:
    """Exactly one addr-spec (no display name, list, quotes or trailing dot). Returns it trimmed."""
    if not isinstance(email, str):
        raise ValueError("A valid email address is required.")
    value = email.strip()
    if not value or len(value) > 254 or not _TRIAL_EMAIL_RE.fullmatch(value):
        raise ValueError("A valid email address is required.")
    return value


def device_fingerprint(public_key_b64: Optional[str] = None) -> Optional[str]:
    """SHA-256 (hex) of the raw device public key: the portal's one-trial key.

    Without an argument it reads this installation's device key, and returns
    None when no device key exists yet (it never creates one).
    """
    if public_key_b64 is None:
        public_key_b64 = _read_existing_device_pubkey()
        if public_key_b64 is None:
            return None
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()


def short_fingerprint(fingerprint: str) -> str:
    """The first 16 hex digits in groups of four, as the portal's email and confirm page show it."""
    head = fingerprint[:16]
    return " ".join(head[i:i + 4] for i in range(0, 16, 4))

from core.license_trial_message import trial_request_message


def build_trial_request(email: str) -> Dict[str, Any]:
    """A Pro trial request for this installation, signed with its device key."""
    request = {
        "v": TRIAL_REQUEST_VERSION,
        "installation_id": get_installation_id(),
        "device_pubkey": get_device_keypair()[1],
        "email": validate_trial_email(email),
        "nonce": base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("="),
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    priv, _pub = get_device_keypair()
    request["signature"] = base64.b64encode(priv.sign(trial_request_message(request))).decode("ascii")
    return request


def trial_request_code(request: Dict[str, Any]) -> str:
    """The offline form of a trial request: pasted on the portal's trial page."""
    raw = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return TRIAL_REQUEST_CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

from core.license_info import LicenseInfo

def _find_license_file() -> Optional[str]:
    """Look for a license file in known locations."""
    def is_file(path):
        try:
            return stat_module.S_ISREG(os.stat(path).st_mode)
        except FileNotFoundError:
            return False
    explicit_path = os.environ.get("ODIN_LICENSE_FILE", "").strip()
    if explicit_path:
        return explicit_path if is_file(explicit_path) else None
    search_paths = [
        os.path.join(LICENSE_DIR, LICENSE_FILENAME),
        os.path.join(os.path.dirname(__file__), LICENSE_FILENAME),
        os.path.join(os.path.dirname(__file__), "..", LICENSE_FILENAME),
        os.path.join("/data", LICENSE_FILENAME),
    ]
    for path in search_paths:
        if is_file(path):
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


def license_binding_is_required() -> bool:
    """Whether this deployment refuses legacy licenses without installation binding."""
    return os.environ.get("ODIN_REQUIRE_LICENSE_BINDING", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _license_expiry_deadline(value: str) -> datetime:
    """Parse signed license expiry without discarding timestamp precision.

    Historical date-only licenses remain valid through the end of that UTC
    date. Timestamped licenses are compared at their exact instant; a missing
    timezone is interpreted as UTC for backward compatibility.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("license expiry must be a non-empty string")
    text = value.strip()
    if "T" not in text:
        return datetime.combine(date.fromisoformat(text), datetime_time.max, tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_clock = None
_clock_directory = None


def license_clock():
    global _clock, _clock_directory
    if _clock is None or _clock_directory != LICENSE_DIR:
        from core.license_clock import LicenseClock, TimeStore
        from core.db import current_engine
        _clock = LicenseClock(TimeStore(LICENSE_DIR, current_engine),
                              wall=lambda: datetime.now(timezone.utc))
        _clock_directory = LICENSE_DIR
    return _clock


def load_license() -> LicenseInfo:
    """Load and validate the installed license file."""
    try:
        path = _find_license_file()
        if not path:
            return LicenseInfo()
        with open(path) as stream:
            raw = stream.read().strip()
    except Exception:
        info = LicenseInfo()
        info.verification_state = "verification_unavailable"
        info.reason_code = "artifact_read_failed"
        info.error = "Failed to read license file"
        return info
    # Verification must never create identity files.
    return verify_license_text(raw)


def _read_existing_installation_id() -> str:
    """Read binding authority without creating or changing installation state."""
    try:
        with open(os.path.join(LICENSE_DIR, INSTALL_ID_FILENAME)) as stream:
            return stream.read().strip()
    except FileNotFoundError:
        return ""


def normalize_license_upload(content: bytes) -> str:
    """Normalize bounded compact/JSON imports without changing installed state."""
    from fastapi import HTTPException
    if len(content) > 1024 * 1024:
        raise HTTPException(status_code=400, detail="License file is too large")
    try:
        license_text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid license file format")

    import json as _json
    try:
        license_json = _json.loads(license_text)
    except RecursionError:
        raise HTTPException(status_code=400, detail="Invalid license file format")
    except ValueError:
        license_json = None
    else:
        if not isinstance(license_json, dict) or not all(
            isinstance(license_json.get(field), str) for field in ("payload", "signature")
        ):
            raise HTTPException(status_code=400, detail="Invalid license file format")
        license_text = license_json["payload"] + "." + license_json["signature"]

    return license_text


def _read_existing_device_pubkey() -> Optional[str]:
    """This installation's device public key, or None when none exists (never creates one)."""
    if not os.path.isfile(os.path.join(LICENSE_DIR, DEVICE_PRIVATE_KEY_FILENAME)):
        return None
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        with open(os.path.join(LICENSE_DIR, DEVICE_PRIVATE_KEY_FILENAME), "rb") as stream:
            private = serialization.load_pem_private_key(stream.read(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            return None
        return base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
    except (OSError, ValueError, TypeError):
        return None


TRIAL_DEVICE_MISMATCH = "This trial license was issued to a different device key. Start or activate the trial from this server."


def verify_license_text(raw: str, installation_id_reader=None, device_pubkey_reader=None) -> LicenseInfo:
    """Verify candidate bytes using runtime rules, without installing a file."""
    info = LicenseInfo()
    info.verification_state = "verification_unavailable"
    info.reason_code = "invalid_artifact"
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
            info.reason_code = "signature_invalid"
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
                info.reason_code = "binding_mismatch"
                info.binding_result = "mismatch"
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

        # Compatibility is the Slice A acceptance contract. Stronger B1 schema
        # checks classify evidence only; they cannot revoke legacy entitlements.
        from core.license_clock import instant
        issued = payload.get("issued_at", "")
        expires = _license_expiry_deadline(payload["expires_at"])
        sample = license_clock().sample()
        clock_error = license_clock().error(issued, sample=sample)
        expired = bool(clock_error) or sample[0] >= expires
        payload_features = payload.get("features")
        features_valid = payload_features is None or (
            isinstance(payload_features, list)
            and all(isinstance(feature, str) for feature in payload_features)
        )
        # Slice A classified expiry before checking the feature-list shape.
        if not expired and not features_valid:
            info.error = "License contains an invalid feature list"
            return info
        try:
            info.issue_deadline = instant(issued) if issued not in ("", None) else None
            evidence_valid = (
                payload["tier"] in TIERS
                and isinstance(payload.get("licensee", ""), (str, type(None)))
                and ("schema_version" not in payload or
                     type(payload["schema_version"]) is int and payload["schema_version"] == 1)
                and all(payload.get(field) is None or
                        type(payload[field]) is int and payload[field] >= 0
                        for field in ("max_printers", "max_users"))
                and all(payload.get(field) is None or isinstance(payload[field], str)
                        for field in ("key", "email", "installation_id"))
                and (info.issue_deadline is None or expires > info.issue_deadline)
                and features_valid
            )
        except (ValueError, TypeError):
            evidence_valid = False

        # Valid license
        tier = payload["tier"]
        try:
            tier_def = TIERS.get(tier, TIERS["community"])
        except TypeError:
            if not expired:
                raise
            # Slice A returned expired before indexing even an unhashable tier.
            tier_def = TIERS["community"]

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
        info.time_confidence = "uncertain" if clock_error else "trusted"
        if evidence_valid:
            info.signed_tier, info.signed_trial = tier, is_trial
            info.signed_max_printers, info.signed_max_users = info.max_printers, info.max_users
            info.verified_digest = info.license_sha256
            info.signed_envelope = raw
            info.binding_result = "matched" if info.binding_present else "not_required"
            info.binding_digest = hashlib.sha256(json.dumps(
                [installation_id_reader(), device_pubkey_reader()], separators=(",", ":")
            ).encode()).hexdigest()
            info.verification_state = "verified_current"
            info.reason_code = "current"
        if clock_error or sample[0] >= expires:
            info.valid = False
            # Compatibility: Slice A treats clock refusal as expired for feature gates.
            info.expired = True
            if evidence_valid:
                info.verification_state = "verification_unavailable" if clock_error else "verified_expired"
                info.reason_code = "clock_uncertain" if clock_error else "deadline_reached"
            info.error = clock_error or f"License expired on {payload['expires_at']}"
            info.max_printers = TIERS["community"]["max_printers"]
            info.max_users = TIERS["community"]["max_users"]
            info.features = TIERS["community"]["features"]
            return info
        info.expired = False

        return info

    except json.JSONDecodeError:
        info.valid = False
        info.error = "License file contains invalid data"
        return info
    except Exception:
        info.valid = False
        info.verification_state = "verification_unavailable"
        info.verified_digest = None
        info.binding_digest = None
        info.error = "License file contains invalid data"
        return info


_mutation_lock = threading.RLock()
_mutation_locks_held = threading.local()


@contextmanager
def license_file_lock(directory=None):
    """Serialize license checks, writes and recovery across threads and workers.

    Lock a stable sidecar, not the license inode (which is atomically replaced).
    Nested installer/removal calls in the owning thread share the lock.
    """
    from fastapi import HTTPException
    target_dir = os.path.realpath(directory or LICENSE_DIR)
    with _mutation_lock:
        held = getattr(_mutation_locks_held, "directories", set())
        if target_dir in held:
            yield
            return
        try:
            os.makedirs(target_dir, exist_ok=True)
            fd = os.open(os.path.join(target_dir, ".odin-license.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            raise HTTPException(status_code=503, detail="Cannot lock the current license safely. The installed license is unchanged.") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _mutation_locks_held.directories = held | {target_dir}
            try:
                yield
            finally:
                _mutation_locks_held.directories = held
        finally:
            os.close(fd)


def save_license_file(content: str, directory: str = None) -> str:
    """Atomically save bytes. Production mutations use install_portal_license
    to serialize validation and recovery; staging itself supports concurrent saves.
    Returns the path.
    """
    target_dir = directory or LICENSE_DIR
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, LICENSE_FILENAME)
    encoded = content.strip().encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".odin-license-", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        # Replacement is visible even if the following durability confirmation fails.
        global _cached_license, _cached_mtime
        _cached_license = None
        _cached_mtime = 0
        directory_fd = os.open(target_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


LICENSE_OLDER_THAN_INSTALLED = (
    "This license file is not newer than the one installed on this server, so it was not installed. "
    "Your installed license is unchanged. If you need an older file, contact support@runsodin.com."
)


LICENSE_MANAGED_OUTSIDE = (
    "This server's license file is managed outside O.D.I.N.: the ODIN_LICENSE_FILE setting points to it, "
    "so it cannot be replaced from here. Replace the file that ODIN_LICENSE_FILE points to. "
    "Your installed license is unchanged."
)


def _directory_entry(path: str) -> str:
    """The path with its folder resolved but its own name not followed (a link stays a link).

    This is the entry ``save_license_file`` replaces.
    """
    return os.path.join(os.path.realpath(os.path.dirname(path) or "."), os.path.basename(path))


def is_expired_replacement(candidate) -> bool:
    """True for a signature-verified license whose end has already passed.

    Only the portal refresh path accepts such a file: after a refund or
    chargeback the portal signs a replacement that has already ended, and ODIN
    installs it so the server stays on Community across restarts. The artifact
    must be fully verified (``verified_expired`` on a trusted clock), carry an
    issue time, and not be a trial. The portal must sign ``expires_at`` strictly
    after ``issued_at``; an equal or inverted pair fails evidence validation and
    is not ``verified_expired``.
    """
    return bool(
        candidate.expired
        and not candidate.trial
        and candidate.verification_state == "verified_expired"
        and candidate.reason_code == "deadline_reached"
        and candidate.issued_at
    )


def _installed_license(previous: Optional[bytes]) -> Optional["LicenseInfo"]:
    """Verify the currently installed bytes (None when nothing is installed)."""
    if previous is None:
        return None
    try:
        return verify_license_text(previous.decode("utf-8").strip())
    except UnicodeDecodeError:
        return None


def _refuse_replacement_out_of_order(candidate, installed) -> None:
    """Refuse a different file for the same key unless its issue time is newer.

    Order is decided by the signed ``issued_at``, never by ``expires_at``: a
    later issue may legitimately end earlier (a cancellation) or already have
    ended (a refund). Equal issue times are accepted only for the same file.
    A file with no issue time cannot be shown to be newer than an
    installed file that has one. Licenses for different keys are not compared.
    """
    from fastapi import HTTPException
    from core.license_clock import instant
    if installed is None or not installed.license_sha256 or not installed.key or installed.key != candidate.key:
        return
    if not installed.issued_at:
        return
    try:
        installed_at = instant(installed.issued_at)
    except ValueError:
        return  # An unreadable installed file cannot be ordered against; never lock the server out.
    try:
        candidate_at = instant(candidate.issued_at) if candidate.issued_at else None
    except ValueError:
        candidate_at = None  # A candidate whose issue time cannot be read is not shown to be newer.
    if (candidate_at is None or candidate_at < installed_at
            or (candidate_at == installed_at and candidate.license_sha256 != installed.license_sha256)):
        raise HTTPException(status_code=409, detail=LICENSE_OLDER_THAN_INSTALLED)


def install_portal_license(license_content, after_install=None, *, allow_expired=False,
                           expected_key=None, expected_sha256=None) -> "LicenseInfo":
    """Verify a portal-issued license and install it only if it is valid.

    The candidate is checked with the runtime rules (signature, binding,
    expiry, payload) BEFORE it touches LICENSE_DIR, so an invalid portal
    response can never replace or delete the installed license.

    A different candidate for the same key not issued after the installed file is
    refused (see ``_refuse_replacement_out_of_order``). With ``allow_expired``
    (the portal refresh path only) a verified replacement that has already
    ended is installed too. Refresh callers pass ``expected_key`` for EVERY
    status. Under the file lock, both the candidate and the file on disk must
    match it; an in-flight refresh cannot overwrite another key or undo removal.
    ``expected_sha256`` also refuses an intervening replacement of the same key
    (including a binding change), so the admin can retry against the new file.
    The file is persisted, so the server stays on Community across restarts.
    """
    from fastapi import HTTPException
    if not isinstance(license_content, str):
        raise HTTPException(status_code=502, detail="License server returned an invalid license file")
    candidate = verify_license_text(license_content.strip())
    if not candidate.valid or candidate.error:
        if not (allow_expired and is_expired_replacement(candidate)):
            raise HTTPException(status_code=400, detail=candidate.error or "License server returned an invalid license")
    with license_file_lock():
        target = os.path.join(LICENSE_DIR, LICENSE_FILENAME)
        try:
            with open(target, "rb") as stream:
                previous = stream.read()
        except FileNotFoundError:
            previous = None
        except OSError:
            raise HTTPException(status_code=503, detail="Cannot read the current license safely. Check storage permissions; the installed license is unchanged.")
        # Identity and issue order are judged against the license that is actually in force,
        # which may live in a supported fallback location; `previous` is only the destination's
        # own content, kept for rollback.
        explicit = os.environ.get("ODIN_LICENSE_FILE", "").strip()
        if explicit and os.path.realpath(explicit) != os.path.realpath(target):
            # The loader reads only that file; a replacement saved here would never take
            # effect, and reporting success would be false (a cancelled plan would stay active).
            raise HTTPException(status_code=409, detail="This server reads its license from the file set in ODIN_LICENSE_FILE, so it cannot be replaced from here. Replace that file, or remove the setting and upload the license. Your installed license is unchanged.")
        if explicit and _directory_entry(target) not in (os.path.realpath(explicit), _directory_entry(explicit)):
            # The destination is only a link to the file ODIN_LICENSE_FILE selects. Saving
            # replaces the link, not that file, so the loader would keep reading the old
            # license while this call reported success (a refunded plan would stay active).
            raise HTTPException(status_code=409, detail=LICENSE_MANAGED_OUTSIDE)
        effective = _find_license_file()
        in_force = previous
        if effective and os.path.realpath(effective) != os.path.realpath(target):
            try:
                with open(effective, "rb") as stream:
                    in_force = stream.read()
            except OSError:
                raise HTTPException(status_code=503, detail="Cannot read the current license safely. Check storage permissions; the installed license is unchanged.")
        installed = _installed_license(in_force)
        if (expected_key is not None or allow_expired) and not (
            installed is not None and installed.license_sha256 and installed.key
            and installed.key == candidate.key
            and (expected_key is None or installed.key == expected_key)
        ):
            raise HTTPException(status_code=400, detail="License server returned a license for a different key than the one installed. Your installed license is unchanged.")
        if expected_sha256 is not None and (
            installed is None or not expected_sha256 or installed.license_sha256 != expected_sha256
        ):
            raise HTTPException(status_code=409, detail="The installed license changed during refresh. Refresh again for the current license.")
        _refuse_replacement_out_of_order(candidate, installed)
        try:
            save_license_file(license_content)
            if after_install is not None:
                after_install(candidate)
        except Exception:
            # save may fail its directory sync after replacement. Restore the old
            # visible artifact as well; never intentionally discard a paid license.
            try:
                try:
                    with open(target, "rb") as stream:
                        unchanged = stream.read() == previous
                except FileNotFoundError:
                    unchanged = previous is None
                if unchanged:
                    pass
                elif previous is not None:
                    fd, recovery = tempfile.mkstemp(prefix=".odin-license-recovery-", dir=LICENSE_DIR)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(previous)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(recovery, target)
                    finally:
                        if os.path.exists(recovery):
                            os.unlink(recovery)
                elif os.path.exists(target):
                    os.unlink(target)
            except OSError:
                raise HTTPException(status_code=503, detail="License recovery could not be confirmed. Check storage and restore the license from your secure host backup.")
            finally:
                global _cached_license
                _cached_license = None
            raise HTTPException(status_code=503, detail="License installation failed; the previous license was retained.")
        return get_license()


@license_file_lock()
def quarantine_installed_license() -> Optional[str]:
    """Atomically move the installed license aside (offline unactivation hand-over).

    The license file is renamed (same directory, atomic) to
    ``odin.license.unactivated-<UTC timestamp>``, which the loader never reads,
    so this install reverts to Community at once. An aborted hand-over is
    recovered only by re-activating with the licensing server; the quarantined
    copy is kept for the operator's records. Returns the quarantine path, or
    None when no license file was installed.
    """
    global _cached_license, _cached_mtime
    path = _find_license_file()
    if not path:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = f"{path}.unactivated-{stamp}"
    os.replace(path, target)
    _cached_license = None
    _cached_mtime = 0
    return target


# ── Cached license (reloaded when file changes) ──
_cached_license: Optional[LicenseInfo] = None
_cached_mtime: float = 0
_cached_identity = None


_cache_lock = threading.RLock()
_cache_checked = 0.0
_cache_verified = 0.0
_cache_retry = 0.0
_cache_failures = 0
_cache_file_identity = None


def get_license(*, persist_clock=True) -> LicenseInfo:
    """Bounded local cache: metadata <=1s, content <=30s, errors 1/5/15/30s."""
    global _cached_license, _cached_mtime, _cached_identity
    global _cache_checked, _cache_verified, _cache_retry, _cache_failures, _cache_file_identity
    with _cache_lock:
        tick = time.monotonic()
        cached = _cached_license
        sample = license_clock().sample(issued_at=cached.issued_at) if cached is not None else (license_clock().wall(), False)
        deadline = bool(cached and cached.valid and cached.expiry_deadline and sample[0] >= cached.expiry_deadline)
        clock_changed = bool(cached and cached.time_confidence != "unavailable" and (sample[1] != (cached.time_confidence == "uncertain")))
        from core.license_lock import enabled
        if enabled() and cached is not None and tick - _cache_checked < 1 and not deadline and not clock_changed:
            return cached
        _cache_checked = tick
        try:
            path = _find_license_file()
            stat = os.stat(path) if path else None
            file_identity = (path, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if stat else None
            identity = []
            for name in (INSTALL_ID_FILENAME, DEVICE_PRIVATE_KEY_FILENAME):
                try:
                    with open(os.path.join(LICENSE_DIR, name), "rb") as stream:
                        identity.append(hashlib.sha256(stream.read()).digest())
                except FileNotFoundError:
                    identity.append(None)
            identity = tuple(identity)
            retry = bool(cached and cached.verification_state == "verification_unavailable" and tick >= _cache_retry)
            if cached is None or file_identity != _cache_file_identity or identity != _cached_identity or deadline or clock_changed or retry or tick - _cache_verified >= 30:
                _cached_license = load_license()
                if _cached_license.verification_state == "community_initial" and enabled():
                    # The private store distinguishes deliberate removal from a
                    # missing previously accepted file, including after restart.
                    from core.db import current_engine
                    from sqlalchemy import text
                    try:
                        with current_engine().connect() as connection:
                            intent = connection.execute(text("SELECT value FROM system_config WHERE key='license_lock_removal_intent'")).scalar()
                            stored = intent or connection.execute(text("SELECT value FROM system_config WHERE key='license_lock_evidence'")).scalar()
                        evidence = json.loads(stored) if isinstance(stored, str) else stored
                    except Exception:
                        evidence = None
                    if isinstance(evidence, dict) and evidence.get("community_selected"):
                        _cached_license.verification_state = "community_selected"
                        _cached_license.reason_code = "explicit_community_selection"
                    elif evidence or cached and cached.verified_digest:
                        _cached_license.verification_state = "verification_unavailable"
                        _cached_license.reason_code = "accepted_artifact_missing"
                _cache_verified = tick
                _cache_file_identity, _cached_identity = file_identity, identity
                _cached_mtime = stat.st_mtime if stat else 0
            if _cached_license.verification_state == "verification_unavailable":
                if _cache_verified == tick:
                    _cache_retry = tick + (1, 5, 15, 30)[min(_cache_failures, 3)]
                    _cache_failures += 1
            else:
                _cache_failures = 0
                license_clock().sample(persist=persist_clock, issued_at=_cached_license.issued_at)
        except (OSError, ValueError, TypeError):
            _cached_license = LicenseInfo()
            _cached_license.verification_state = "verification_unavailable"
            _cached_license.reason_code = "storage_or_identity_unavailable"
            _cached_license.error = "License storage or binding identity is unavailable"
            _cache_retry = tick + (1, 5, 15, 30)[min(_cache_failures, 3)]
            _cache_failures += 1
        return _cached_license


def require_feature(feature: str):
    """Check if a feature is available. Raises if not."""
    from fastapi import HTTPException
    license_info = get_license()
    if not license_info.has_feature(feature):
        tier = license_info.tier
        eligible = [
            tier_def["name"]
            for tier_name, tier_def in TIERS.items()
            if tier_name != "community" and feature in tier_def["features"]
        ]
        requirement = " or ".join(eligible) if eligible else "a license with this entitlement"
        raise HTTPException(
            status_code=403,
            detail=f"Feature '{feature}' requires {requirement}. Current tier: {tier}",
            headers={"X-ODIN-License-Feature": feature},
        )


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def feature_gate(feature: str, *, keep_existing_records: bool = False):
    """FastAPI dependency enforcing ``require_feature`` for a route family.

    ``keep_existing_records=True`` is the downgrade/expiry policy for
    record-keeping features (orders, products, maintenance): a license that
    lapses must never cost an install its own data, so reads/exports (GET,
    HEAD, OPTIONS) and deletion of existing records stay available on every
    tier, while creating or changing records (POST, PUT, PATCH) requires the
    entitlement. Without it every method is gated (computed features such as
    analytics, and configuration such as branding).
    """
    from fastapi import Request

    def dependency(request: Request) -> None:
        if keep_existing_records and (request.method in _READ_METHODS or request.method == "DELETE"):
            return
        require_feature(feature)

    dependency.__name__ = f"require_feature_{feature}"
    return dependency


def license_is_managed_externally() -> bool:
    """Whether license lifecycle is controlled by a read-only deployment mount."""
    return os.environ.get("ODIN_LICENSE_READ_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def require_license_mutation_enabled() -> None:
    """FastAPI dependency that blocks mutation of externally managed licenses."""
    if license_is_managed_externally():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail="License is externally managed by this deployment and cannot be changed here.",
        )


from license_limits import check_printer_limit, check_user_limit

ENTERPRISE_ONLY_FEATURES = frozenset({"controlled_parts"})

def enterprise_feature_allowed(license_info, feature):
    return license_info.valid and license_info.tier == "enterprise"
