"""
modules/push/routes/biometric.py — Biometric refresh token endpoints.

Face ID / Touch ID silent re-authentication flow:
  1. User logs in with password → standard JWT (24h)
  2. POST /auth/biometric-token → 30-day refresh token stored server-side
  3. App open: Face ID gate → POST /auth/biometric-refresh → new JWT (no password)
  4. Logout: DELETE /auth/biometric-token → token revoked

The current token rotates on use. A bounded retry can recover the same successor.
Only the SHA-256 hash of the current token is stored.
"""

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text, update

from core import auth, license_lock
from core.audit import log_audit
from core.models import AuditLog
from core.db import get_db
from core.dependencies import get_current_user
from core.rbac import require_interactive_session
from modules.push.models import BiometricToken
from modules.push.schemas import (
    BiometricTokenResponse,
    BiometricRefreshRequest,
    BiometricRefreshResponse,
)

log = logging.getLogger("push.biometric")
router = APIRouter(tags=["Authentication"])

BIOMETRIC_TOKEN_TTL_DAYS = 30
BIOMETRIC_RETRY_SECONDS = 600
BIOMETRIC_RETRY_MAX = 5
_ROTATION_LABEL = b"odin-biometric-rotate-v1"
_UNKNOWN = "Invalid or revoked biometric token"
_SAVED_SIGN_IN_EXPIRED = "This device's saved sign-in has expired. Sign in with your password."
_RETRY = "auth.biometric_retry_used"
_CAP = "auth.biometric_retry_cap_refused"
_LATE = "auth.biometric_late_retry_refused"
_REPLAY = "auth.biometric_replay_refused"


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/auth/biometric-token", response_model=BiometricTokenResponse, dependencies=[Depends(require_interactive_session())])
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

    license_lock.authorize_user(db, current_user["id"], issuance=True)
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


def _rotation_key():
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_ROTATION_LABEL).derive(
        auth.SECRET_KEY.encode()
    )


def _successor(key, row_id, raw):
    message = _ROTATION_LABEL + row_id.to_bytes(8, "big") + raw.encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _classify(row, raw, token_hash, key):
    if hmac.compare_digest(row.token_hash, token_hash):
        return "current", None
    if row.last_used_at is not None:
        successor = _successor(key, row.id, raw)
        if hmac.compare_digest(row.token_hash, _hash_token(successor)):
            return "predecessor", successor
        if hmac.compare_digest(row.token_hash, _hash_token(_successor(key, row.id, successor))):
            return "grand-predecessor", None
    return "unknown", None


def _lock_user(db, user_id):
    if license_lock.enabled():
        license_lock._lock(db)
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        db.execute(text('UPDATE users SET id=id WHERE id=:id'), {'id': user_id})
    suffix = ' FOR UPDATE NOWAIT' if dialect == 'postgresql' else ''
    return db.execute(
        text('SELECT * FROM users WHERE id=:id AND is_active IS TRUE' + suffix),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant lock suffix
        {'id': user_id},
    ).fetchone()


def _check_expiry(row):
    if row.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(401, "Biometric token expired — please log in again")


def _authorize(db, user):
    if user is None:
        raise HTTPException(401, "User not found or inactive")
    license_lock.authorize_user(db, user.id, issuance=True)


def _issue(db, user, request, raw, expiry):
    from core.session_tracking import stage_session
    access = auth.create_access_token(data={"sub": user.username, "role": user.role})
    if stage_session(db, user.id, access, request.client.host if request.client else "unknown",
                     request.headers.get("user-agent", "")) is not True:
        raise RuntimeError("Session staging refused")
    db.commit()
    return BiometricRefreshResponse(access_token=access, refresh_token=raw, expires_at=expiry)


def _rotate(db, row, user, body, token_hash, key, request):
    _authorize(db, user)
    _check_expiry(row)
    raw = _successor(key, row.id, body.refresh_token)
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=BIOMETRIC_TOKEN_TTL_DAYS)
    rotated = db.execute(
        update(BiometricToken).where(
            BiometricToken.id == row.id,
            BiometricToken.user_id == user.id,
            BiometricToken.device_id == body.device_id,
            BiometricToken.token_hash == token_hash,
            BiometricToken.is_revoked.is_(False),
            BiometricToken.expires_at == row.expires_at,
        ).values(token_hash=_hash_token(raw), expires_at=expiry.replace(tzinfo=None),
                 last_used_at=now.replace(tzinfo=None))
        .execution_options(synchronize_session=False)
    )
    if rotated.rowcount == 1:
        return _issue(db, user, request, raw, expiry)
    db.rollback()
    return None


def _audit_count(db, row, action):
    return db.query(AuditLog).filter(
        AuditLog.entity_type == "user", AuditLog.entity_id == row.user_id,
        AuditLog.action == action, AuditLog.timestamp >= row.last_used_at,
        AuditLog.details["device_row_id"].as_integer() == row.id,
    ).count()


def _audit(db, row, action, now, age):
    entry = log_audit(db, action, "user", row.user_id, {
        "device_id": row.device_id[:8], "device_row_id": row.id, "seconds_since_rotation": max(0, int(age)),
    })
    # SQLite's CURRENT_TIMESTAMP truncates to seconds. Keep rotation precision.
    # A worker clock behind the rotation must still deduplicate its refusal.
    entry.timestamp = max(now.replace(tzinfo=None), row.last_used_at.replace(tzinfo=None))


def _retry(db, body, token_hash, key, request):
    candidates = db.query(BiometricToken).filter(
        BiometricToken.device_id == body.device_id, BiometricToken.is_revoked.is_(False),
    )
    for candidate in candidates:
        kind, _ = _classify(candidate, body.refresh_token, token_hash, key)
        if kind != "unknown":
            row_id, user_id = candidate.id, candidate.user_id
            break
    else:
        raise HTTPException(401, _UNKNOWN)

    db.rollback()
    user = _lock_user(db, user_id)
    # Sign-out and re-enrolment delete the credential without locking its user.
    row = (
        candidates.filter(BiometricToken.id == row_id, BiometricToken.user_id == user_id)
        .with_for_update(nowait=True).populate_existing().one_or_none()
    )
    if row is None:
        raise HTTPException(401, _UNKNOWN)
    kind, successor = _classify(row, body.refresh_token, token_hash, key)
    if kind == "unknown":
        raise HTTPException(401, _UNKNOWN)
    if kind == "current":
        response = _rotate(db, row, user, body, token_hash, key, request)
        if response is None:
            raise HTTPException(401, _UNKNOWN)
        return response

    now = datetime.now(timezone.utc)
    age = (now - row.last_used_at.replace(tzinfo=timezone.utc)).total_seconds()
    if kind == "grand-predecessor":
        action = _REPLAY
    elif not 0 <= age <= BIOMETRIC_RETRY_SECONDS:
        action = _LATE
    elif _audit_count(db, row, _RETRY) >= BIOMETRIC_RETRY_MAX:
        action = _CAP
    else:
        _check_expiry(row)
        _authorize(db, user)
        _audit(db, row, _RETRY, now, age)
        return _issue(db, user, request, successor, row.expires_at.replace(tzinfo=timezone.utc))

    if not _audit_count(db, row, action):
        _audit(db, row, action, now, age)
        db.commit()
    raise HTTPException(401, _SAVED_SIGN_IN_EXPIRED)


@router.post("/auth/biometric-refresh", response_model=BiometricRefreshResponse)
async def refresh_with_biometric_token(
    body: BiometricRefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Renew a biometric sign-in, including a bounded retry after a lost answer."""
    if not re.fullmatch(r"[0-9a-f]{64}", body.refresh_token):
        raise HTTPException(401, _UNKNOWN)
    token_hash = _hash_token(body.refresh_token)
    try:
        row = db.query(BiometricToken).filter(
            BiometricToken.token_hash == token_hash,
            BiometricToken.device_id == body.device_id,
            BiometricToken.is_revoked.is_(False),
        ).first()
        key = _rotation_key()
        if row is not None:
            _check_expiry(row)
            response = _rotate(db, row, _lock_user(db, row.user_id), body, token_hash, key, request)
            if response is not None:
                return response
        return _retry(db, body, token_hash, key, request)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(503, "Login temporarily unavailable. Try again later.") from None


@router.delete("/auth/biometric-token", status_code=204, dependencies=[Depends(require_interactive_session())])
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
