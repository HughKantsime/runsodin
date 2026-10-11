"""Shared allowlist and parsing for scoped API credentials."""
import json

VALID_SCOPES = {
    "read", "write",
    "read:printers", "write:printers",
    "read:jobs", "write:jobs",
    "read:spools", "write:spools",
    "read:models", "write:models",
    "read:analytics",
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
    """Broad permissions cover their resources, never the reverse direction."""
    if required in scopes or "admin" in scopes:
        return True
    action, separator, resource = required.partition(":")
    return bool(separator and resource and action in ("read", "write") and action in scopes)
