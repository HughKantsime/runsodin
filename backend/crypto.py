"""
Encryption utilities for sensitive data like API keys.

Uses Fernet symmetric encryption. Keys are encrypted at rest
and decrypted only when needed.
"""

import os
import base64
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken


def get_fernet() -> Optional[Fernet]:
    """Get Fernet instance using encryption key from environment."""
    key = os.environ.get('ENCRYPTION_KEY')
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None


def generate_key() -> str:
    """Generate a new encryption key. Run once and save to .env."""
    return Fernet.generate_key().decode()


def encrypt(plaintext: str) -> Optional[str]:
    """
    Encrypt a string. Returns base64-encoded ciphertext.
    Returns None if encryption is not configured.
    """
    if not plaintext:
        return plaintext
    
    fernet = get_fernet()
    if not fernet:
        # No encryption key configured, return as-is
        # (for backwards compatibility)
        return plaintext
    
    try:
        encrypted = fernet.encrypt(plaintext.encode())
        return encrypted.decode()
    except Exception:
        return plaintext


def decrypt(ciphertext: str) -> Optional[str]:
    """
    Decrypt a string. Returns plaintext.
    Returns original string if decryption fails (might be unencrypted).
    """
    if not ciphertext:
        return ciphertext
    
    fernet = get_fernet()
    if not fernet:
        # No encryption key configured, return as-is
        return ciphertext
    
    try:
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()
    except InvalidToken:
        # Might be an old unencrypted value, return as-is
        return ciphertext
    except Exception:
        return ciphertext


def is_encrypted(value: str) -> bool:
    """Check if a value appears to be Fernet-encrypted."""
    if not value:
        return False
    try:
        # Fernet tokens are base64 and start with 'gAAAAA'
        return value.startswith('gAAAAA') and len(value) > 50
    except Exception:
        return False


if __name__ == "__main__":
    # Helper to generate a new key
    print("New encryption key:")
    print(generate_key())
    print("\nAdd this to your .env file as:")
    print("ENCRYPTION_KEY=<key>")
