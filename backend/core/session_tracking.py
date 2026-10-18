"""Confirmed persistence for newly issued interactive sessions."""
import logging
import time
import jwt
from sqlalchemy import text
from core import auth
from core.db_compat import sql

log=logging.getLogger("odin.api")


def stage_session(db,user_id,access_token,ip,user_agent,enrollment_jti=None):
    """Stage a matching session; caller must commit, or roll back on False.

    True never authorizes releasing a token before the caller commits.
    This helper never commits or rolls back the caller transaction.
    """
    try:
        payload=jwt.decode(access_token,auth.SECRET_KEY,algorithms=[auth.ALGORITHM],
                           options={"require":["exp","sub","jti"]})
        subject=payload["sub"];jti=payload["jti"]
        if (not isinstance(subject,str) or not subject or not isinstance(jti,str) or not jti
                or "mfa_pending" in payload or "mfa_setup_required" in payload):
            return False
        if enrollment_jti is not None:
            if not isinstance(enrollment_jti,str) or not enrollment_jti:
                return False
            from core.mfa_login import _locked_user
            user=_locked_user(db,user_id)
            if not user or not user.is_active or not user.mfa_enabled or user.username!=subject:
                return False
            authority=db.execute(text("SELECT expires_at FROM mfa_enrollments WHERE jti=:jti AND user_id=:uid AND state='consumed' AND encrypted_pending_secret IS NULL"),{'jti':enrollment_jti,'uid':user_id}).fetchone()
            if not authority or authority.expires_at<=time.time():
                return False
        user=db.execute(text("SELECT username,is_active FROM users WHERE id=:uid"),{"uid":user_id}).fetchone()
        if not user or not user.is_active or user.username!=subject:
            return False
        db.execute(text(f"{sql.insert_or_ignore_prefix()} active_sessions "  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only sql.* dialect helpers with literal arguments are interpolated; values are bound
                        "(user_id,token_jti,ip_address,user_agent) VALUES(:uid,:jti,:ip,:ua)"
                        f"{sql.on_conflict_ignore('token_jti')}"),
                   {"uid":user_id,"jti":jti,"ip":ip,"ua":(user_agent or "")[:500]})
        row=db.execute(text("SELECT user_id FROM active_sessions WHERE token_jti=:jti"),{"jti":jti}).fetchone()
        if not row or row.user_id!=user_id:
            return False
        return True
    except Exception:
        log.warning("Session staging could not be confirmed")
        return False


def record_session(db,user_id,access_token,ip,user_agent,enrollment_jti=None):
    """True only after matching active identity/session binding commits."""
    try:
        if stage_session(db,user_id,access_token,ip,user_agent,enrollment_jti) is not True:
            db.rollback()
            return False
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        log.warning("Session persistence could not be confirmed")
        return False
