"""
O.D.I.N. — Auth helper utilities.

Low-level helpers for login rate limiting, account lockout, and
password complexity validation. Operate directly on SQLite (not SQLAlchemy)
to avoid session/transaction conflicts with the monitor daemons.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import _validate_password) continues to work via re-exports in deps.py.
"""

import logging
import os
import time as _time

log = logging.getLogger("odin.api")

# ── Constants ──────────────────────────────────────────────────────────────────
_LOGIN_RATE_LIMIT = 10               # max attempts per window
_LOGIN_RATE_WINDOW = 300             # 5 minute window
_LOCKOUT_THRESHOLD = 5               # failed attempts before lockout
_LOCKOUT_DURATION = 900              # 15 minute lockout
_last_login_cleanup = 0              # timestamp of last cleanup


def _login_db_path() -> str:
    return os.environ.get("DATABASE_PATH", "/data/odin.db")


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


def _check_rate_limit(ip: str) -> bool:
    """Returns True if rate limited. Queries login_attempts table."""
    try:
        import sqlite3
        conn = sqlite3.connect(_login_db_path(), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        now = _time.time()
        cur = conn.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE ip = ? AND success = 0 AND attempted_at > ?",
            (ip, now - _LOGIN_RATE_WINDOW),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count >= _LOGIN_RATE_LIMIT
    except Exception:
        return False  # fail open on DB errors


def _record_login_attempt(ip: str, username: str, success: bool, db=None):
    """Record attempt for rate limiting and audit."""
    # Persist to login_attempts table
    try:
        import sqlite3
        conn = sqlite3.connect(_login_db_path(), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            "INSERT INTO login_attempts (ip, username, attempted_at, success) "
            "VALUES (?, ?, ?, ?)",
            (ip, username, _time.time(), 1 if success else 0),
        )
        conn.commit()
        conn.close()
    except Exception:
        log.warning("Failed to persist login attempt", exc_info=True)

    # Lazy cleanup every hour
    global _last_login_cleanup
    now = _time.time()
    if now - _last_login_cleanup > 3600:
        _last_login_cleanup = now
        try:
            import sqlite3
            conn = sqlite3.connect(_login_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                "DELETE FROM login_attempts WHERE attempted_at < ?",
                (now - _LOCKOUT_DURATION * 2,),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # Log to audit trail if db available
    if db:
        from models import AuditLog
        try:
            audit_entry = AuditLog(
                action="login_success" if success else "login_failed",
                entity_type="user",
                details=f"{'Login' if success else 'Failed login'}: {username} from {ip}",
                ip_address=ip,
            )
            db.add(audit_entry)
            db.commit()
        except Exception:
            log.warning("Failed to record login audit entry", exc_info=True)


def _is_locked_out(username: str) -> bool:
    """Returns True if account is locked (>= LOCKOUT_THRESHOLD failures in window)."""
    try:
        import sqlite3
        conn = sqlite3.connect(_login_db_path(), timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        now = _time.time()
        cur = conn.execute(
            "SELECT COUNT(*) FROM login_attempts "
            "WHERE username = ? AND success = 0 AND attempted_at > ?",
            (username, now - _LOCKOUT_DURATION),
        )
        count = cur.fetchone()[0]
        conn.close()
        return count >= _LOCKOUT_THRESHOLD
    except Exception:
        return False  # fail open on DB errors
