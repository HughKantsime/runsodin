"""Local VAPID key pair for Web Push: one source of truth in system_config['vapid_keys'].

The public half is what /push/vapid-key hands browsers as applicationServerKey;
every push sender signs with the private half of the same pair. The pair is
generated locally (P-256, no external key service) on first need and persisted.
"""

import base64
import json
import logging
from typing import Optional

from sqlalchemy import JSON, bindparam, text

log = logging.getLogger("odin.notifications")

CONFIG_KEY = "vapid_keys"


def decode_config_value(value):
    """system_config.value is a JSON column: SQLite raw reads return text,
    PostgreSQL returns it already decoded."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


def _valid(keys) -> bool:
    return (isinstance(keys, dict) and isinstance(keys.get("public_key"), str) and keys["public_key"]
            and isinstance(keys.get("private_key"), str) and keys["private_key"])


def read_vapid_keys(executor) -> Optional[dict]:
    """Stored key pair, or None when absent or malformed. ``executor`` is a
    SQLAlchemy Session or Connection."""
    row = executor.execute(text("SELECT value FROM system_config WHERE key = :key"), {"key": CONFIG_KEY}).fetchone()
    if not row:
        return None
    try:
        keys = decode_config_value(row[0])
    except (TypeError, ValueError):
        return None
    return keys if _valid(keys) else None


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_vapid_keys() -> dict:
    """A fresh P-256 pair in the encodings pywebpush and PushManager expect."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    return {"private_key": _b64u(private.private_numbers().private_value.to_bytes(32, "big")),
            "public_key": _b64u(public)}


def ensure_vapid_keys(db) -> dict:
    """Return the stored pair, generating and persisting one if none exists.

    Concurrent first calls race on the primary key; the loser keeps the
    winner's pair (INSERT ... ON CONFLICT DO NOTHING, then re-read). A stored
    but malformed value is left untouched and reported, never overwritten.
    """
    keys = read_vapid_keys(db)
    if keys:
        return keys
    exists = db.execute(text("SELECT 1 FROM system_config WHERE key = :key"), {"key": CONFIG_KEY}).fetchone()
    if exists:
        raise ValueError("system_config['vapid_keys'] is malformed; refusing to replace it")
    statement = text(
        "INSERT INTO system_config(key, value) VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
    ).bindparams(bindparam("value", type_=JSON))
    db.execute(statement, {"key": CONFIG_KEY, "value": generate_vapid_keys()})
    db.commit()
    log.info("Generated local VAPID key pair for Web Push")
    keys = read_vapid_keys(db)
    if not keys:
        raise ValueError("VAPID key pair could not be persisted")
    return keys
