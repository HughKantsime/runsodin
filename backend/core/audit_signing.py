"""Read the existing installation identity without creating or changing it."""
import base64
import hashlib
from pathlib import Path
from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def existing_device_key():
    import license_manager as lm
    try:
        key = serialization.load_pem_private_key(
            (Path(lm.LICENSE_DIR)/lm.DEVICE_PRIVATE_KEY_FILENAME).read_bytes(), password=None)
        installation = lm._read_existing_installation_id()
        if not isinstance(key, Ed25519PrivateKey) or not installation:
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise HTTPException(409, 'Existing installation device identity required for signed export') from None
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(public).decode(), installation, hashlib.sha256(public).hexdigest()
