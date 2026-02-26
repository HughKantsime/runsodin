"""
Encryption utilities for sensitive data like API keys.

Re-export facade: canonical location is now backend/core/crypto.py.
All existing `from crypto import ...` or `import crypto` imports continue to work.
"""

from core.crypto import (  # noqa: F401
    get_fernet,
    generate_key,
    encrypt,
    decrypt,
    is_encrypted,
)
