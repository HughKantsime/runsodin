"""
Authentication utilities for O.D.I.N.
Handles password hashing, JWT tokens, and user verification.

Re-export facade: canonical location is now backend/core/auth.py.
All existing `from auth import ...` imports continue to work.
"""

from core.auth import (  # noqa: F401
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_HOURS,
    pwd_context,
    Token,
    TokenData,
    UserCreate,
    UserResponse,
    verify_password,
    hash_password,
    create_access_token,
    decode_token,
    ROLE_HIERARCHY,
    has_permission,
)
