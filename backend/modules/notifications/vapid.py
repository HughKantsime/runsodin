"""VAPID key pair for Web Push: one effective pair for serving and signing.

Precedence (industry-standard explicit override):
1. Environment: ``VAPID_PRIVATE_KEY`` (raw base64url P-256 scalar or PEM) wins when
   set. The served public key is derived from it, or taken from ``VAPID_PUBLIC_KEY``
   when the private key's format cannot be derived. Rotation through the API is
   refused while an override is set (rotate the environment instead).
2. Stored: ``system_config['vapid_keys']`` = {"public_key", "private_key"}, the
   private half Fernet-encrypted with core.crypto (like the SMTP/MQTT secrets).
   Legacy plaintext private keys are read transparently.
3. Generated: on first need a P-256 pair is generated locally (no external key
   service) and stored encrypted.

Rotation generates a new pair and deletes every push subscription (they are bound
to the old applicationServerKey); browsers resubscribe when they see the new key.
"""

import base64
import json
import logging
import os
from typing import Optional

from sqlalchemy import JSON, bindparam, text

log = logging.getLogger("odin.notifications")

CONFIG_KEY = "vapid_keys"


class VapidOverrideActive(RuntimeError):
    """Raised when an API rotation is attempted while VAPID_PRIVATE_KEY overrides storage."""


def decode_config_value(value):
    """system_config.value is a JSON column: SQLite raw reads return text,
    PostgreSQL returns it already decoded."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_from_private(private_value: str) -> Optional[str]:
    """Uncompressed public point (base64url) for a raw base64url scalar or a PEM key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        if "BEGIN" in private_value:
            key = serialization.load_pem_private_key(private_value.encode(), password=None)
        else:
            raw = _unb64u(private_value.strip())
            if len(raw) != 32:
                return None
            key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            return None
        return _b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint))
    except Exception:
        return None


def env_override() -> Optional[dict]:
    """The environment pair when VAPID_PRIVATE_KEY is set (public may be None if underivable)."""
    private = os.environ.get("VAPID_PRIVATE_KEY")
    if not private:
        return None
    public = _public_from_private(private) or os.environ.get("VAPID_PUBLIC_KEY") or None
    return {"public_key": public, "private_key": private, "source": "env"}


def _decrypt_private(value: str) -> Optional[str]:
    import core.crypto as crypto
    if crypto.is_encrypted(value):
        plain = crypto.decrypt(value)
        # crypto.decrypt returns the input unchanged when it cannot decrypt.
        return None if plain == value else plain
    return value  # legacy plaintext


def keys_from_stored_value(value) -> Optional[dict]:
    """Decode a stored system_config value into a usable pair, or None."""
    try:
        keys = decode_config_value(value)
    except (TypeError, ValueError):
        return None
    if not (isinstance(keys, dict) and isinstance(keys.get("public_key"), str) and keys["public_key"]
            and isinstance(keys.get("private_key"), str) and keys["private_key"]):
        return None
    private = _decrypt_private(keys["private_key"])
    if not private:
        log.error("Stored VAPID private key cannot be decrypted with the configured ENCRYPTION_KEY")
        return None
    return {"public_key": keys["public_key"], "private_key": private, "source": "stored"}


def read_vapid_keys(executor) -> Optional[dict]:
    """Effective pair: environment override, else the stored pair; None when neither."""
    override = env_override()
    if override:
        return override
    row = executor.execute(text("SELECT value FROM system_config WHERE key = :key"), {"key": CONFIG_KEY}).fetchone()
    return keys_from_stored_value(row[0]) if row else None


def generate_vapid_keys() -> dict:
    """A fresh P-256 pair in the encodings pywebpush and PushManager expect."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    return {"private_key": _b64u(private.private_numbers().private_value.to_bytes(32, "big")),
            "public_key": _b64u(public)}


def _stored_form(keys: dict) -> dict:
    import core.crypto as crypto
    return {"public_key": keys["public_key"], "private_key": crypto.encrypt(keys["private_key"])}


def ensure_vapid_keys(db) -> dict:
    """Return the effective pair, generating and persisting (encrypted) one if none exists.

    Concurrent first calls race on the primary key; the loser keeps the
    winner's pair (INSERT ... ON CONFLICT DO NOTHING, then re-read). A stored
    but unusable value is left untouched and reported, never overwritten.
    """
    keys = read_vapid_keys(db)
    if keys:
        return keys
    exists = db.execute(text("SELECT 1 FROM system_config WHERE key = :key"), {"key": CONFIG_KEY}).fetchone()
    if exists:
        raise ValueError("system_config['vapid_keys'] is unusable; refusing to replace it (rotate explicitly)")
    statement = text(
        "INSERT INTO system_config(key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
    ).bindparams(bindparam("value", type_=JSON))
    db.execute(statement, {"key": CONFIG_KEY, "value": _stored_form(generate_vapid_keys())})
    db.commit()
    log.info("Generated local VAPID key pair for Web Push")
    keys = read_vapid_keys(db)
    if not keys:
        raise ValueError("VAPID key pair could not be persisted")
    return keys


def rotate_vapid_keys(db) -> dict:
    """Replace the stored pair and invalidate every push subscription (caller commits)."""
    if env_override():
        raise VapidOverrideActive("VAPID_PRIVATE_KEY is set in the environment; rotate it there")
    keys = generate_vapid_keys()
    value = bindparam("value", type_=JSON)
    updated = db.execute(text("UPDATE system_config SET value = :value WHERE key = :key").bindparams(value),
                         {"key": CONFIG_KEY, "value": _stored_form(keys)}).rowcount
    if not updated:
        db.execute(text("INSERT INTO system_config(key, value) VALUES (:key, :value)").bindparams(value),
                   {"key": CONFIG_KEY, "value": _stored_form(keys)})
    removed = db.execute(text("DELETE FROM push_subscriptions")).rowcount or 0
    log.warning("VAPID key pair rotated; %s push subscriptions invalidated", removed)
    return {"public_key": keys["public_key"], "subscriptions_invalidated": removed}
