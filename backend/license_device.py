"""License helpers; public tier and verification authority stays in license_manager."""
from __future__ import annotations
import json
import base64
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional, Dict, Any

def get_device_keypair():
    """Return this instance's Ed25519 (private, public) device keypair.

    Proof-of-possession gate for license operations (S1 from 2026-04-12
    adversarial review). The private key is persisted at
    ``{LICENSE_DIR}/.odin-device.key`` with file mode 0600, so only the
    ODIN server process on this host can sign license operations.

    If the file doesn't exist yet (new install), generate a fresh keypair
    and store it. Subsequent calls read the same material.

    The public half is sent to the license server on activation and bound
    to the activation row; unactivation / reactivation requests must
    carry a signature this pubkey can verify.

    Returns (private_key_pem: bytes, public_key_b64: str).
    """
    if not owner.CRYPTO_AVAILABLE:
        raise RuntimeError(
            "cryptography package is required for license device keys. "
            "pip install cryptography"
        )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization

    path = os.path.join(owner.LICENSE_DIR, owner.DEVICE_PRIVATE_KEY_FILENAME)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            priv_pem = f.read()
        priv = serialization.load_pem_private_key(priv_pem, password=None)
    else:
        # Generate fresh keypair. Store with 0600 so only this process's
        # user can read it — that's our trust boundary for "the machine
        # holds the license."
        priv = Ed25519PrivateKey.generate()
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        os.makedirs(owner.LICENSE_DIR, exist_ok=True)
        # Write atomically with the right mode — don't leave a world-
        # readable file on disk for even a moment.
        tmp_path = path + ".tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(priv_pem)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        os.rename(tmp_path, path)

    pub = priv.public_key()
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    return priv, pub_b64


def sign_license_challenge(action: str, key: str, installation_id: str, nonce: str) -> str:
    """Sign a license-operation challenge with this device's private key.

    Returns base64 Ed25519 signature. Canonical message format MUST match
    api/_lib/license-pop.js's canonicalMessage() on the license server:

        odin-license-v1\\n${action}\\n${key}\\n${installation_id}\\n${nonce}

    action ∈ {"unactivate", "reactivate", "activate-bootstrap"}.
    """
    if action not in ("unactivate", "reactivate", "activate-bootstrap"):
        raise ValueError(f"unknown action: {action}")
    priv, _pub = owner.get_device_keypair()
    message = f"odin-license-v1\n{action}\n{key}\n{installation_id}\n{nonce}".encode("utf-8")
    sig = priv.sign(message)
    return base64.b64encode(sig).decode("ascii")


def validate_trial_email(email: str) -> str:
    """Exactly one addr-spec (no display name, list, quotes or trailing dot). Returns it trimmed."""
    if not isinstance(email, str):
        raise ValueError("A valid email address is required.")
    value = email.strip()
    if not value or len(value) > 254 or not owner._TRIAL_EMAIL_RE.fullmatch(value):
        raise ValueError("A valid email address is required.")
    return value


def device_fingerprint(public_key_b64: Optional[str] = None) -> Optional[str]:
    """SHA-256 (hex) of the raw device public key: the portal's one-trial key.

    Without an argument it reads this installation's device key, and returns
    None when no device key exists yet (it never creates one).
    """
    if public_key_b64 is None:
        public_key_b64 = owner._read_existing_device_pubkey()
        if public_key_b64 is None:
            return None
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()


def short_fingerprint(fingerprint: str) -> str:
    """The first 16 hex digits in groups of four, as the portal's email and confirm page show it."""
    head = fingerprint[:16]
    return " ".join(head[i:i + 4] for i in range(0, 16, 4))


def trial_request_message(request: Dict[str, Any]) -> bytes:
    return "\n".join([
        "odin-trial-request-v1", request["installation_id"], request["device_pubkey"],
        request["email"], request["nonce"], request["issued_at"],
    ]).encode("utf-8")


def build_trial_request(email: str) -> Dict[str, Any]:
    """A Pro trial request for this installation, signed with its device key."""
    request = {
        "v": owner.TRIAL_REQUEST_VERSION,
        "installation_id": owner.get_installation_id(),
        "device_pubkey": owner.get_device_keypair()[1],
        "email": owner.validate_trial_email(email),
        "nonce": base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("="),
        "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    priv, _pub = owner.get_device_keypair()
    request["signature"] = base64.b64encode(priv.sign(owner.trial_request_message(request))).decode("ascii")
    return request


def trial_request_code(request: Dict[str, Any]) -> str:
    """The offline form of a trial request: pasted on the portal's trial page."""
    raw = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return owner.TRIAL_REQUEST_CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


import license_manager as owner
