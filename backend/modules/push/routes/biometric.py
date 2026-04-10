"""
modules/push/routes/biometric.py — Biometric refresh token endpoints.

Face ID / Touch ID silent re-authentication flow:
  1. User logs in with password → standard JWT (24h)
  2. POST /auth/biometric-token → 30-day refresh token stored server-side
  3. App open: Face ID gate → POST /auth/biometric-refresh → new JWT (no password)
  4. Logout: DELETE /auth/biometric-token → token revoked

Tokens are rolling: each refresh issues a NEW token and revokes the old one.
Token value is a random UUID; only the SHA-256 hash is stored.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.auth import create_access_token
from core.db import get_db
from core.dependencies import get_current_user
from modules.push.models import BiometricToken
from modules.push.schemas import (
    BiometricTokenResponse,
    BiometricRefreshRequest,
    BiometricRefreshResponse,
)

log = logging.getLogger("push.biometric")
router = APIRouter(tags=["Authentication"])

BIOMETRIC_TOKEN_TTL_DAYS = 30


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/auth/biometric-token", response_model=BiometricTokenResponse)
async def create_biometric_token(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Exchange a valid JWT for a 30-day biometric refresh token.

    Called once after password login when the user enables Face ID / Touch ID.
    One token per device — replaces any existing token for this device.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    raw_token = secrets.token_hex(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=BIOMETRIC_TOKEN_TTL_DAYS)

    # Replace any existing token for this device
    db.query(BiometricToken).filter(
        BiometricToken.user_id == current_user["id"],
        BiometricToken.device_id == device_id,
    ).delete()

    bio_token = BiometricToken(
        user_id=current_user["id"],
        device_id=device_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(bio_token)
    db.commit()

    log.info(f"Biometric token issued for user={current_user['id']} device={device_id[:8]}…")
    return BiometricTokenResponse(
        refresh_token=raw_token,
        expires_at=expires_at,
        device_id=device_id,
    )


@router.post("/auth/biometric-refresh", response_model=BiometricRefreshResponse)
async def refresh_with_biometric_token(
    body: BiometricRefreshRequest,
    db: Session = Depends(get_db),
):
    """
    Exchange a biometric refresh token for a new JWT — no password required.

    Called on every app open after Face ID / Touch ID passes.
    Issues a new rolling refresh token; revokes the one used.
    """
    token_hash = _hash_token(body.refresh_token)

    bio_token = (
        db.query(BiometricToken)
        .filter(
            BiometricToken.token_hash == token_hash,
            BiometricToken.device_id == body.device_id,
            BiometricToken.is_revoked == False,  # noqa: E712
        )
        .first()
    )

    if not bio_token:
        raise HTTPException(status_code=401, detail="Invalid or revoked biometric token")

    if bio_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        bio_token.is_revoked = True
        db.commit()
        raise HTTPException(status_code=401, detail="Biometric token expired — please log in again")

    # Look up the user
    user_row = db.execute(
        text("SELECT * FROM users WHERE id = :id AND is_active = 1"),
        {"id": bio_token.user_id},
    ).fetchone()
    if not user_row:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Rolling: revoke current token, issue new one
    bio_token.is_revoked = True
    bio_token.last_used_at = datetime.now(timezone.utc)

    new_raw = secrets.token_hex(32)
    new_hash = _hash_token(new_raw)
    new_expires = datetime.now(timezone.utc) + timedelta(days=BIOMETRIC_TOKEN_TTL_DAYS)

    new_token = BiometricToken(
        user_id=bio_token.user_id,
        device_id=body.device_id,
        token_hash=new_hash,
        expires_at=new_expires,
    )
    db.add(new_token)
    db.commit()

    # Issue new JWT
    access_token = create_access_token(
        data={"sub": user_row.username, "role": user_row.role}
    )

    log.info(f"Biometric refresh: user={bio_token.user_id} device={body.device_id[:8]}…")
    return BiometricRefreshResponse(access_token=access_token)


@router.delete("/auth/biometric-token", status_code=204)
async def revoke_biometric_token(
    device_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke all biometric tokens for a device. Called on logout."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    db.query(BiometricToken).filter(
        BiometricToken.user_id == current_user["id"],
        BiometricToken.device_id == device_id,
    ).delete()
    db.commit()
    log.info(f"Biometric token revoked: user={current_user['id']} device={device_id[:8]}…")
