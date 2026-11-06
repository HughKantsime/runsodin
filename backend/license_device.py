"""Device identity helpers, retaining license_manager as the public API."""
import base64
import os
import uuid
import license_manager as manager

def get_installation_id() -> str:
    """Read the persistent installation ID. Generates one if missing (defensive)."""
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
    """Use the canonical device-key policy exposed by the license facade."""
    return manager.get_device_keypair()


def sign_license_challenge(action: str, key: str, installation_id: str, nonce: str) -> str:
    """Sign a license-operation challenge with this device's private key.

    Returns base64 Ed25519 signature. Canonical message format MUST match
    api/_lib/license-pop.js's canonicalMessage() on the license server:

        odin-license-v1\\n${action}\\n${key}\\n${installation_id}\\n${nonce}

    action ∈ {"unactivate", "reactivate", "activate-bootstrap"}.
    """
    if action not in ("unactivate", "reactivate", "activate-bootstrap"):
        raise ValueError(f"unknown action: {action}")
    priv, _pub = manager.get_device_keypair()
    message = f"odin-license-v1\n{action}\n{key}\n{installation_id}\n{nonce}".encode("utf-8")
    sig = priv.sign(message)
    return base64.b64encode(sig).decode("ascii")


