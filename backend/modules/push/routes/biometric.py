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

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text, update

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
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Exchange a biometric refresh token for a new JWT — no password required.

    Called on every app open after Face ID / Touch ID passes.
    Issues a new rolling refresh token; revokes the one used.
    """
    token_hash = _hash_token(body.refresh_token)

    try:
        bio_token = (
            db.query(BiometricToken)
            .filter(
                BiometricToken.token_hash == token_hash,
                BiometricToken.device_id == body.device_id,
                BiometricToken.is_revoked == False,  # noqa: E712
            )
            .first()
        )
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail='Login temporarily unavailable. Try again later.') from None

    if not bio_token:
        raise HTTPException(status_code=401, detail="Invalid or revoked biometric token")

    if bio_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        db.rollback()
        raise HTTPException(status_code=401, detail="Biometric token expired — please log in again")

    # Acquire parent authority before changing its credential. SQLite's exact
    # no-op write also fences a transaction that already performed discovery.
    try:
        if db.get_bind().dialect.name == 'sqlite':
            db.execute(text('UPDATE users SET id=id WHERE id=:id'), {'id': bio_token.user_id})
        suffix = ' FOR UPDATE NOWAIT' if db.get_bind().dialect.name == 'postgresql' else ''
        user_row = db.execute(
            text('SELECT * FROM users WHERE id=:id AND is_active IS TRUE' + suffix),
            {'id': bio_token.user_id},
        ).fetchone()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail='Login temporarily unavailable. Try again later.') from None
    if not user_row:
        db.rollback()
        raise HTTPException(status_code=401, detail='User not found or inactive')

    original_id = bio_token.id
    original_expiry = bio_token.expires_at
    if original_expiry.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        db.rollback()
        raise HTTPException(status_code=401, detail='Biometric token expired; please log in again')
    new_raw = secrets.token_hex(32)
    new_expires = datetime.now(timezone.utc) + timedelta(days=BIOMETRIC_TOKEN_TTL_DAYS)
    try:
        rotated = db.execute(
            update(BiometricToken).where(
                BiometricToken.id == original_id,
                BiometricToken.user_id == user_row.id,
                BiometricToken.device_id == body.device_id,
                BiometricToken.token_hash == token_hash,
                BiometricToken.is_revoked.is_(False),
                BiometricToken.expires_at == original_expiry,
            ).values(token_hash=_hash_token(new_raw), expires_at=new_expires,
                     last_used_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        )
        if rotated.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=401, detail='Invalid or revoked biometric token')
        access_token = create_access_token(data={'sub': user_row.username, 'role': user_row.role})
        from core.session_tracking import stage_session
        if stage_session(db, user_row.id, access_token,
                         request.client.host if request.client else 'unknown',
                         request.headers.get('user-agent', '')) is not True:
            raise RuntimeError('Session staging refused')
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail='Login temporarily unavailable. Try again later.') from None
    return BiometricRefreshResponse(access_token=access_token, refresh_token=new_raw, expires_at=new_expires)

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
