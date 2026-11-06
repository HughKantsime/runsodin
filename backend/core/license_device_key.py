"""Installation Ed25519 key persistence; called by the license facade."""
import os
import base64

def get_device_keypair(directory, filename, crypto_available):
    """Return this instance's Ed25519 (private, public) device keypair.

    Proof-of-possession gate for license operations (S1 from 2026-04-12
    adversarial review). The private key is persisted at
    ``{directory}/.odin-device.key`` with file mode 0600, so only the
    ODIN server process on this host can sign license operations.

    If the file doesn't exist yet (new install), generate a fresh keypair
    and store it. Subsequent calls read the same material.

    The public half is sent to the license server on activation and bound
    to the activation row; unactivation / reactivation requests must
    carry a signature this pubkey can verify.

    Returns (private_key_pem: bytes, public_key_b64: str).
    """
    if not crypto_available:
        raise RuntimeError(
            "cryptography package is required for license device keys. "
            "pip install cryptography"
        )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization

    path = os.path.join(directory, filename)
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
        os.makedirs(directory, exist_ok=True)
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

