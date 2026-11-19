"""Account-wide MFA failures. All checks serialize on the enrolled user's row."""
import json
import logging
import math
from functools import wraps
from time import time

import pyotp
from fastapi import HTTPException
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from core.crypto import decrypt
from core.mfa_login import _locked_user, decode_login_challenge
from core.rate_limit import limiter
from modules.organizations.mfa_recovery import consume_code, recovery_key

log = logging.getLogger(__name__)

MFA_ADDRESS_LIMIT = "10/minute"
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60


def lock_for_code_check(db, user_id):
    # Finish the preliminary identity reads. From here until the code check
    # succeeds, the only writes to commit are failure accounting and its audit.
    db.rollback()
    return _locked_user(db, user_id)


def clear_failures(db, user_id):
    db.execute(text("DELETE FROM mfa_code_attempts WHERE user_id=:uid"), {"uid": user_id})


def _locked(seconds):
    minutes = math.ceil(seconds / 60)
    unit = "minute" if minutes == 1 else "minutes"
    return HTTPException(429, f"Too many wrong codes. Try again in {minutes} {unit}.",
                         headers={"Retry-After": str(math.ceil(seconds))})


def unlocked_failures(db, user_id):
    stored = db.execute(text("SELECT failure_times FROM mfa_code_attempts WHERE user_id=:uid"),
                        {"uid": user_id}).scalar_one_or_none()
    if stored is None:
        return []
    try:
        failures = json.loads(stored)
        if not isinstance(failures, list) or not all(
            type(t) is int or (isinstance(t, float) and math.isfinite(t)) for t in failures
        ):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        log.warning("Ignoring malformed MFA failure history")
        return []
    now = time()
    failures = [t for t in failures if t <= now]
    if len(failures) >= MAX_FAILURES and failures[-1] > now - LOCKOUT_SECONDS:
        raise _locked(LOCKOUT_SECONDS - (now - failures[-1]))
    return failures


def limit_code_checks(handler):
    """Apply the address limit, preserving an authenticated account's lockout notice."""
    limited = limiter.limit(MFA_ADDRESS_LIMIT)(handler)

    @wraps(handler)
    async def wrapped(*args, **kwargs):
        try:
            return await limited(*args, **kwargs)
        except RateLimitExceeded as address_error:
            # Both limits can apply. Only disclose the account countdown to a
            # signed-in caller or the holder of a valid, unconsumed challenge.
            db = kwargs["db"]
            if "current_user" in kwargs:
                user_id = kwargs["current_user"]["id"]
            else:
                try:
                    payload, _ = decode_login_challenge(kwargs["body"].get("mfa_token"))
                except HTTPException:
                    raise address_error from None
                user_id = db.execute(text(
                    "SELECT id FROM users WHERE username=:sub AND is_active IS TRUE AND mfa_enabled IS TRUE "
                    "AND NOT EXISTS (SELECT 1 FROM token_blacklist WHERE jti=:jti)"
                ), {"sub": payload["sub"], "jti": payload["jti"]}).scalar_one_or_none()
            if user_id is not None:
                unlocked_failures(db, user_id)
            raise

    return wrapped


def valid_totp(user, code):
    return pyotp.TOTP(decrypt(user.mfa_secret)).verify(code, valid_window=1)


def require_code(db, request, user, value, *, recovery=False, regenerate=False):
    """Check under the user lock, before staging challenge or credential changes."""
    from core.audit import log_audit

    failures = unlocked_failures(db, user.id)

    # Regeneration accepts either kind in its single code field, and requires
    # working encryption even for an account with a legacy plaintext TOTP secret.
    key = None
    totp = ''.join(value.split()) if isinstance(value, str) and not recovery else value
    if regenerate:
        key = recovery_key()
        recovery = not (isinstance(totp, str) and len(totp) == 6 and totp.isascii() and totp.isdigit())
    accepted = consume_code(db, user.id, value) if recovery else valid_totp(user, totp)
    if accepted:
        clear_failures(db, user.id)
        return key

    now = time()
    failures = [t for t in failures if t > now - FAILURE_WINDOW_SECONDS]
    failures.append(now)
    db.execute(text("INSERT INTO mfa_code_attempts(user_id,failure_times) VALUES(:uid,:times) "
                    "ON CONFLICT(user_id) DO UPDATE SET failure_times=excluded.failure_times"),
               {"uid": user.id, "times": json.dumps(failures)})
    db.commit()  # The lock must survive an audit failure.
    if len(failures) == MAX_FAILURES:
        try:
            log_audit(db, "mfa_code_lockout", "user", user.id, {"route": request.scope["route"].path},
                      ip=request.client.host if request.client else "unknown")
            db.commit()
        except Exception:
            db.rollback()
            log.warning("Could not audit MFA code lockout")
        raise _locked(LOCKOUT_SECONDS)
    raise HTTPException(401, "Invalid MFA code")
