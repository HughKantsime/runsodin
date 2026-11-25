"""Square setup grants. The caller commits the account, scope and audit changes together."""
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text

from core.api_token_policy import REMOVE_INELIGIBLE_SETUP_SQL, SET_TOKEN_SCOPES_SQL, SQUARE_SETUP_SCOPE, parse_token_scopes
from core.audit import log_audit
from core.db_compat import as_utc_datetime


def lock_accounts(db, administrator_id, owner_id):
    # Lock in the same order for reciprocal revocations by two administrators.
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("UPDATE users SET id=id WHERE id=:id"), {"id": administrator_id})
    statement = "SELECT id, role, is_active, group_id FROM users WHERE id IN (:admin, :owner) ORDER BY id"
    if db.get_bind().dialect.name == "postgresql":
        statement += " FOR UPDATE"
    rows = db.execute(text(statement), {"admin": administrator_id, "owner": owner_id}).mappings()
    return {row["id"]: row for row in rows}


def check_grant_expiry(expires_at):
    if expires_at is None:
        return
    try:
        if as_utc_datetime(expires_at) > datetime.now(timezone.utc):
            return
    except (TypeError, ValueError, OverflowError):
        pass
    raise HTTPException(409, "This token has expired. Create a new token to grant Square setup access.")


def set_scopes(db, row, scopes, enabled, actor, reason=None):
    updated = [scope for scope in scopes if scope != SQUARE_SETUP_SCOPE]
    if enabled:
        updated.append(SQUARE_SETUP_SCOPE)
    # Hard deletion and erasure delete the credential itself in this transaction.
    if reason not in ("owner_deleted", "owner_erased"):
        statement = REMOVE_INELIGIBLE_SETUP_SQL if reason else SET_TOKEN_SCOPES_SQL
        changed = db.execute(text(statement), {"scopes": json.dumps(updated), "id": row.id,
                                               "uid": row.user_id, "previous": row.scopes})
        if changed.rowcount != 1:
            raise HTTPException(409, "Token changed. Reload the token list before continuing.")
    details = {"token_id": row.id}
    if reason != "owner_erased":
        details.update(administrator_id=actor["id"], token_name=row.name, token_owner_user_id=row.user_id,
                       auth_kind=actor.get("_auth_kind"), scopes_before=scopes, scopes_after=updated)
    if reason:
        details["reason"] = reason
    log_audit(db, "api_token.square_setup_granted" if enabled else "api_token.square_setup_revoked",
              "api_token", row.id, details)
    return updated


def revoke_owner_grants(db, user_id, actor, reason):
    """The account update or deletion already holds the owner lock."""
    rows = db.execute(text("SELECT id, name, user_id, scopes FROM api_tokens WHERE user_id=:uid"),
                      {"uid": user_id}).all()
    for row in rows:
        scopes = parse_token_scopes(row.scopes) or []
        if SQUARE_SETUP_SCOPE in scopes:
            set_scopes(db, row, scopes, False, actor, reason)
