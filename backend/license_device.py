"""Persistent device identity helpers, re-exported by license_manager."""
import base64
import os
import uuid


def get_installation_id() -> str:
    """Read the persistent installation ID. Generates one if missing (defensive)."""
    import license_manager as manager
    id_path = os.path.join(manager.LICENSE_DIR, manager.INSTALL_ID_FILENAME)
    if os.path.isfile(id_path):
        return open(id_path).read().strip()
    # Defensive: generate if file was deleted
    install_id = str(uuid.uuid4())
    os.makedirs(manager.LICENSE_DIR, exist_ok=True)
    with open(id_path, "w") as f:
        f.write(install_id)
    return install_id


def get_device_keypair():
    """Return this instance's Ed25519 (private, public) device keypair.

    Proof-of-possession gate for license operations (S1 from 2026-04-12
    adversarial review). The private key is persisted at
    ``{manager.LICENSE_DIR}/.odin-device.key`` with file mode 0600, so only the
    ODIN server process on this host can sign license operations.

    If the file doesn't exist yet (new install), generate a fresh keypair
    and store it. Subsequent calls read the same material.

    The public half is sent to the license server on activation and bound
    to the activation row; unactivation / reactivation requests must
    carry a signature this pubkey can verify.

    Returns (private_key_pem: bytes, public_key_b64: str).
    """
    import license_manager as manager
    if not manager.CRYPTO_AVAILABLE:
        raise RuntimeError(
            "cryptography package is required for license device keys. "
            "pip install cryptography"
        )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization

    path = os.path.join(manager.LICENSE_DIR, manager.DEVICE_PRIVATE_KEY_FILENAME)
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
        os.makedirs(manager.LICENSE_DIR, exist_ok=True)
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
    import license_manager as manager
    if action not in ("unactivate", "reactivate", "activate-bootstrap"):
        raise ValueError(f"unknown action: {action}")
    priv, _pub = manager.get_device_keypair()
    message = f"odin-license-v1\n{action}\n{key}\n{installation_id}\n{nonce}".encode("utf-8")
    sig = priv.sign(message)
    return base64.b64encode(sig).decode("ascii")


