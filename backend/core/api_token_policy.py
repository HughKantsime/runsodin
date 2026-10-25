"""Shared allowlist and parsing for scoped API credentials."""
import json

VALID_SCOPES = {
    "read", "write",
    # Explicit opt-in for inspected stock operations; broad write/admin do not imply it.
    "write:production",
    "read:printers", "write:printers",
    "read:jobs", "write:jobs",
    "read:spools", "write:spools",
    "read:models", "write:models",
    "read:analytics",
    # Camera images only (the OBS overlay stream); grants no other read.
    "read:cameras",
    "read:education", "write:education",
    # Phase 2 (v1.9.0): agent-surface scopes for the MCP tool layer.
    # agent:write is strictly narrower than `write` — it grants the
    # 11 advertised write tools and nothing else. See
    # core/rbac.py::require_any_scope for enforcement. Minting these
    # is role-gated below (viewers can mint agent:read; only
    # operator+ can mint agent:write).
    "agent:read", "agent:write",
    "admin",
}

def parse_token_scopes(value):
    try:
        scopes = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None
    if not isinstance(scopes, list) or not scopes or any(not isinstance(scope, str) or scope not in VALID_SCOPES for scope in scopes):
        return None
    return scopes


def token_scope_granted(scopes, required):
    """Broad permissions imply resources except explicit production delegation."""
    if required == "write:production":
        return required in scopes
    if required in scopes or "admin" in scopes:
        return True
    action, separator, resource = required.partition(":")
    return bool(separator and resource and action in ("read", "write") and action in scopes)


# Scopes that authenticate only the OBS overlay camera relay. A token holding
# nothing else is not a general credential: get_current_user refuses it on
# every route, whatever scope (or none) the route declares.
CAMERA_ONLY_SCOPES = frozenset({"read:cameras"})
CAMERA_TOKEN_DEFAULT_DAYS = 365  # default lifetime of a camera-only token


# read:cameras is strictly camera-only: it must be the token's only scope. A
# token that also reads printers (or everything) would be a general credential
# sitting in an OBS URL, and the overlay relay accepts nothing but camera-only
# tokens.


def camera_scope_combination_error(scopes):
    """Mint-time rule; returns an error message or None."""
    if "read:cameras" not in scopes:
        return None
    if set(scopes) != CAMERA_ONLY_SCOPES:
        return "read:cameras must be the token's only scope"
    return None


def camera_token_expiry_error(expires_days):
    """Camera tokens never outlive the default lifetime; a longer request is refused."""
    if isinstance(expires_days, int) and not isinstance(expires_days, bool) and expires_days > CAMERA_TOKEN_DEFAULT_DAYS:
        return f"read:cameras tokens expire within {CAMERA_TOKEN_DEFAULT_DAYS} days"
    return None


def is_camera_only(scopes) -> bool:
    return bool(scopes) and set(scopes) <= CAMERA_ONLY_SCOPES


def is_legacy_camera_combination(scopes) -> bool:
    """read:cameras together with other scopes: mintable only before the camera-only
    rule (never in a release). Such tokens are revoked."""
    return bool(scopes) and "read:cameras" in scopes and set(scopes) != CAMERA_ONLY_SCOPES


def revoke_legacy_camera_tokens(db) -> int:
    """Delete (ODIN's token revocation) every combined read:cameras token, with an audit
    row each. Idempotent; runs at startup. Returns how many were revoked."""
    from sqlalchemy import text
    from core.audit import log_audit
    rows = db.execute(text("SELECT id, user_id, scopes FROM api_tokens WHERE scopes LIKE :pattern"),
                      {"pattern": "%read:cameras%"}).fetchall()
    revoked = [row for row in rows if is_legacy_camera_combination(parse_token_scopes(row.scopes) or [])]
    for row in revoked:
        db.execute(text("DELETE FROM api_tokens WHERE id = :id"), {"id": row.id})
        log_audit(db, "api_token_revoked", "api_token", row.id,
                  {"reason": "legacy_camera_token", "user_id": row.user_id})
    db.commit()
    return len(revoked)
