"""Runtime, fail-closed guard: API-token requests cannot write credential material.

Route dependencies (``require_interactive_session``) are the first line. This is
the second: once ``get_current_user`` resolves a non-interactive principal (any
per-user ``odin_`` token, or the legacy global API key), the request context is
marked, and every SQL statement executed in that context is inspected at the
cursor. A write to authentication or account-recovery material is refused with
403 however the code is shaped: raw SQL in a constant or f-string, an imported
helper, an ORM attribute change or ``db.add``, ``update(User)``. ORM flushes are
covered because they emit SQL through the same cursor.

Background daemons, the CLI and requests authenticated by a login session are
never marked, so they are unaffected.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.engine import Engine

CREDENTIAL_WRITE_DETAIL = "API tokens cannot create or change credentials; sign in with a password session"

_NONINTERACTIVE: ContextVar[bool] = ContextVar("odin_noninteractive_principal", default=False)

# Whole tables that hold authentication material.
GUARDED_TABLES = frozenset({
    "api_tokens", "biometric_tokens", "active_sessions", "push_devices", "push_subscriptions",
    "oidc_config", "oidc_auth_codes", "password_reset_tokens",
})
# users: only credential / account-recovery columns (role, is_active, group_id, theme stay writable).
USERS_CREDENTIAL_COLUMNS = frozenset({
    "email", "username", "password_hash", "mfa_enabled", "mfa_secret",
    "oidc_issuer", "oidc_subject", "oidc_provider",
})
# system_config keys that carry credential, transport or setup material.
GUARDED_CONFIG_KEYS = frozenset({
    "smtp_config", "base_url", "setup_complete", "setup_admin_claimed", "require_mfa",
})
GUARDED_CONFIG_PREFIXES = ("oidc",)
# Deletes that change authentication behaviour itself (SSO config, setup/MFA/SMTP keys).
_DELETE_GUARDED_TABLES = frozenset({"oidc_config", "system_config"})
# Narrow telemetry writes the token-authentication path itself performs.
_ALLOWED_TOKEN_TELEMETRY = {("api_tokens", frozenset({"last_used_at"}))}

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_WRITE = re.compile(
    r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM|MERGE\s+INTO)\s+"
    r"((?:\"?\w+\"?\.)?\"?\w+\"?)", re.I)
_SET_COLUMN = re.compile(r"(?:^|,|\()\s*\"?(\w+)\"?\s*=", re.I)


class CredentialWriteRefused(HTTPException):
    def __init__(self, target: str):
        super().__init__(status_code=403, detail=CREDENTIAL_WRITE_DETAIL)
        self.target = target


def mark_noninteractive_principal() -> None:
    """Called by get_current_user when it returns an API-token principal."""
    _NONINTERACTIVE.set(True)


def request_is_noninteractive() -> bool:
    return _NONINTERACTIVE.get()


def _table_name(raw: str) -> str:
    return raw.split(".")[-1].strip('"').lower()


def _set_columns(sql: str, start: int) -> frozenset[str]:
    """Columns assigned by the UPDATE starting at ``start`` (up to WHERE/RETURNING/;)."""
    tail = sql[start:]
    m = re.search(r"\bSET\b(.*?)(?:\bWHERE\b|\bRETURNING\b|\bFROM\b|;|$)", tail, re.I | re.S)
    if not m:
        return frozenset({"*"})
    clause = m.group(1)
    if re.match(r"\s*\(", clause):  # tuple SET (a, b) = (...)
        names = re.match(r"\s*\(([^)]*)\)", clause).group(1)
        return frozenset(n.strip().strip('"').lower() for n in names.split(","))
    return frozenset(c.lower() for c in _SET_COLUMN.findall(clause))


def _insert_columns(sql: str, start: int) -> frozenset[str]:
    tail = sql[start:]
    m = re.match(r"[^(]*?\(([^)]*)\)\s*(?:VALUES|SELECT)", tail, re.I | re.S)
    if not m:
        return frozenset({"*"})  # no column list: assume every column
    return frozenset(n.strip().strip('"').lower() for n in m.group(1).split(","))


def _parameter_values(parameters: Any) -> Iterable[Any]:
    if isinstance(parameters, dict):
        yield from parameters.values()
    elif isinstance(parameters, (list, tuple)):
        for item in parameters:
            if isinstance(item, (dict, list, tuple)):
                yield from _parameter_values(item)
            else:
                yield item


def _is_guarded_config_key(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    key = value.strip().lower()
    return key in GUARDED_CONFIG_KEYS or key.startswith(GUARDED_CONFIG_PREFIXES)


def guarded_write_target(statement: str, parameters: Any) -> str | None:
    """Return the guarded target a statement writes, or None."""
    sql = _COMMENT.sub(" ", statement or "")
    for match in _WRITE.finditer(sql):
        verb = match.group(1).split()[0].upper()
        table = _table_name(match.group(2))
        if verb == "DELETE" and table not in _DELETE_GUARDED_TABLES:
            # Removing rows (user delete, token revoke) withdraws access; it cannot
            # mint or redirect a credential. Route gates still govern who may do it.
            continue
        if verb == "UPDATE":
            columns = _set_columns(sql, match.end())
        elif verb in {"INSERT", "REPLACE", "MERGE"}:
            columns = _insert_columns(sql, match.end())
        else:
            columns = frozenset({"*"})
        # Upserts can also assign in ON CONFLICT / ON DUPLICATE KEY.
        conflict = re.search(r"\bDO\s+UPDATE\s+SET\b|\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", sql[match.end():], re.I)
        if conflict:
            columns = columns | _set_columns(sql, match.end() + conflict.start())
        if table in GUARDED_TABLES:
            if (table, columns) in _ALLOWED_TOKEN_TELEMETRY:
                continue
            return table
        if table == "users":
            if verb in {"INSERT", "REPLACE", "MERGE"} or "*" in columns or columns & USERS_CREDENTIAL_COLUMNS:
                return "users"
        if table == "system_config":
            literal_keys = re.findall(r"'([^']*)'", sql[match.start():])
            if any(_is_guarded_config_key(k) for k in literal_keys) or any(
                    _is_guarded_config_key(v) for v in _parameter_values(parameters)):
                return "system_config"
    return None


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    if not _NONINTERACTIVE.get():
        return
    target = guarded_write_target(statement, parameters)
    if target is not None:
        raise CredentialWriteRefused(target)


_INSTALLED = False


def install_credential_write_guard() -> None:
    """Attach the guard to every Engine in this process (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
    _INSTALLED = True
