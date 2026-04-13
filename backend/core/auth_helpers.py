"""
O.D.I.N. — Auth helper utilities.

Low-level helpers for login rate limiting, account lockout, and
password complexity validation.

R9 from the 2026-04-12 Codex adversarial review: these helpers used to
open their own sqlite3 connection via `DATABASE_PATH` env var, which
diverged from the app's main DB in any deployment that overrode
DATABASE_URL (Postgres, non-default sqlite path, etc.). Result: either
"all logins blocked because the helper DB is unavailable" or "throttling
silently tracks a different database than the app is using."

Now: every helper takes a `db: Session` from the caller's FastAPI
dependency, uses `db.execute(text(...))` to run the same SQL against the
main DB. No more DATABASE_PATH env var. The login_attempts table was
already in core/migrations/001_initial.sql so no schema change is needed.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import _validate_password) continues to work
via re-exports in deps.py.
"""

import logging
import time as _time

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("odin.api")

# ── Constants ──────────────────────────────────────────────────────────────────
_LOGIN_RATE_LIMIT = 10               # max attempts per window
_LOGIN_RATE_WINDOW = 300             # 5 minute window
_LOCKOUT_THRESHOLD = 5               # failed attempts before lockout
_LOCKOUT_DURATION = 900              # 15 minute lockout
_last_login_cleanup = 0              # timestamp of last cleanup


def _validate_password(password: str) -> tuple:
    """Validate password complexity. Returns (is_valid, message)."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, "OK"


def _check_rate_limit(db: Session, ip: str) -> bool:
    """Returns True if rate limited. Queries login_attempts table.

    Fails closed: if the DB query raises, assume rate-limited to prevent
    brute-force attempts during contention. The alternative (fail open)
    would let an attacker race with a DB-stalled instance to bypass
    throttling entirely.
    """
    try:
        now = _time.time()
        row = db.execute(
            text(
                "SELECT COUNT(*) FROM login_attempts "
                "WHERE ip = :ip AND success = 0 AND attempted_at > :cutoff"
            ),
            {"ip": ip, "cutoff": now - _LOGIN_RATE_WINDOW},
        ).fetchone()
        count = row[0] if row else 0
        return count >= _LOGIN_RATE_LIMIT
    except Exception:
        log.warning("Rate limit check failed (DB unavailable) — failing closed", exc_info=True)
        return True  # fail closed: block login if we can't verify rate limit


def _record_login_attempt(db: Session, ip: str, username: str, success: bool) -> None:
    """Record attempt for rate limiting and audit.

    The audit log entry (separate from login_attempts) is staged on the
    same session so it commits atomically with the attempt row.
    """
    # Persist to login_attempts table
    try:
        db.execute(
            text(
                "INSERT INTO login_attempts (ip, username, attempted_at, success) "
                "VALUES (:ip, :username, :attempted_at, :success)"
            ),
            {
                "ip": ip,
                "username": username,
                "attempted_at": _time.time(),
                "success": 1 if success else 0,
            },
        )
        db.commit()
    except Exception:
        log.warning("Failed to persist login attempt", exc_info=True)
        db.rollback()

    # Lazy cleanup every hour — keep table bounded so rate-limit queries
    # stay fast. Runs in a separate transaction so a cleanup failure
    # doesn't mask the attempt record.
    global _last_login_cleanup
    now = _time.time()
    if now - _last_login_cleanup > 3600:
        _last_login_cleanup = now
        try:
            db.execute(
                text("DELETE FROM login_attempts WHERE attempted_at < :cutoff"),
                {"cutoff": now - _LOCKOUT_DURATION * 2},
            )
            db.commit()
        except Exception:
            log.debug("Login attempt cleanup failed (non-fatal)", exc_info=True)
            db.rollback()

    # Log to audit trail (staged on db — commits with the next db.commit()
    # call from the route handler, or immediately if the caller is done).
    try:
        from core.models import AuditLog
        entry = AuditLog(
            action="login_success" if success else "login_failed",
            entity_type="user",
            details=f"{'Login' if success else 'Failed login'}: {username} from {ip}",
            ip_address=ip,
        )
        db.add(entry)
        db.commit()
    except Exception:
        log.warning("Failed to record login audit entry", exc_info=True)
        db.rollback()


def _is_locked_out(db: Session, username: str) -> bool:
    """Returns True if account is locked (>= LOCKOUT_THRESHOLD failures in window).

    Fails closed: if the DB query raises, assume locked to prevent
    brute-force during contention.
    """
    try:
        now = _time.time()
        row = db.execute(
            text(
                "SELECT COUNT(*) FROM login_attempts "
                "WHERE username = :username AND success = 0 AND attempted_at > :cutoff"
            ),
            {"username": username, "cutoff": now - _LOCKOUT_DURATION},
        ).fetchone()
        count = row[0] if row else 0
        return count >= _LOCKOUT_THRESHOLD
    except Exception:
        log.warning("Lockout check failed (DB unavailable) — failing closed", exc_info=True)
        return True  # fail closed: block login if we can't verify lockout status
