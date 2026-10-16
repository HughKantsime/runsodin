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
never marked, so they are unaffected. Raw DBAPI cursors from core.db_utils.get_db
are wrapped at install time, and ORM SystemConfig rows are checked by key at flush.

Known limits (no current code path relies on them): work handed to a
ThreadPoolExecutor/threading.Thread/run_in_executor does not inherit the request
context, and statements on connections opened outside SQLAlchemy and db_utils
are not seen.
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
_IDENT = r"[\"`\[]?\w+[\"`\]]?"
_WRITE = re.compile(
    r"\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM|MERGE\s+INTO)\s+"
    rf"((?:{_IDENT}\.)?{_IDENT})", re.I)
_SET_STOP = re.compile(r"(WHERE|RETURNING|FROM)\b", re.I)
# system_config writes must name their key exactly; LIKE/GLOB/concatenation fail closed.
_ORM_PK_WHERE = re.compile(r"\bWHERE\s+\"?system_config\"?\.\"?id\"?\s*=", re.I)
_KEY_EQUALS = re.compile(r"\bkey\b[\"`\]]?\s*=\s*(?:'[^']*'|:\w+|\?|%\(\w+\)s|\$\d+)", re.I)


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
    return raw.split(".")[-1].strip('"`[]').lower()


def _top_level_set_clause(sql: str, start: int) -> str | None:
    """The SET clause after ``start``, cut at the first top-level WHERE/RETURNING/FROM/;.
    Parentheses and quotes are tracked so subqueries inside SET don't end it early."""
    m = re.search(r"\bSET\b", sql[start:], re.I)
    if not m:
        return None
    i = start + m.end()
    depth, quote, out = 0, None, []
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (ch == ";" or (sql[i - 1:i].isspace() and _SET_STOP.match(sql, i))):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _split_top_level(clause: str) -> list[str]:
    parts, depth, quote, cur = [], 0, None, []
    for ch in clause:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def _set_columns(sql: str, start: int) -> frozenset[str]:
    """Columns an UPDATE assigns. Self-assignments (``SET id = id``, the row-lock idiom)
    change nothing and are left out; an empty result means a no-op lock."""
    clause = _top_level_set_clause(sql, start)
    if clause is None:
        return frozenset({"*"})
    tup = re.match(r"\s*\(([^)]*)\)\s*=", clause)
    if tup:  # tuple SET (a, b) = (...)
        return frozenset(n.strip().strip('"`[]').lower() for n in tup.group(1).split(","))
    columns = set()
    for part in _split_top_level(clause):
        lhs, eq, rhs = part.partition("=")
        if not eq:
            continue
        name = lhs.strip().split(".")[-1].strip('"`[]').lower()
        if rhs.strip().split(".")[-1].strip('"`[]').lower() == name:
            continue  # x = x
        columns.add(name)
    return frozenset(columns)


def _insert_columns(sql: str, start: int) -> frozenset[str]:
    tail = sql[start:]
    m = re.match(r"[^(]*?\(([^)]*)\)\s*(?:VALUES|SELECT)", tail, re.I | re.S)
    if not m:
        return frozenset({"*"})  # no column list: assume every column
    return frozenset(n.strip().strip('"').lower() for n in m.group(1).split(","))


def _parameter_values(parameters: Any) -> Iterable[tuple[str | None, Any]]:
    """(name, value) pairs; name is None for positional parameters."""
    if isinstance(parameters, dict):
        for name, value in parameters.items():
            yield str(name), value
    elif isinstance(parameters, (list, tuple)):
        for item in parameters:
            if isinstance(item, (dict, list, tuple)):
                yield from _parameter_values(item)
            else:
                yield None, item


def _is_guarded_config_key(value: Any, *, prefix: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    key = value.strip().lower()
    return key in GUARDED_CONFIG_KEYS or (prefix and key.startswith(GUARDED_CONFIG_PREFIXES))


def _config_param_is_guarded(name: str | None, value: Any) -> bool:
    # A parameter named like a key is matched by prefix too; other values only
    # exactly, so an ordinary value such as a theme "oidc-blue" is not mistaken.
    return _is_guarded_config_key(value, prefix=name is None or "key" in name.lower())


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
        if verb == "UPDATE" and not columns:
            continue  # SET x = x row-lock idiom: writes nothing
        if table in GUARDED_TABLES:
            if (table, columns) in _ALLOWED_TOKEN_TELEMETRY:
                continue
            return table
        if table == "users":
            if verb in {"INSERT", "REPLACE", "MERGE"} or "*" in columns or columns & USERS_CREDENTIAL_COLUMNS:
                return "users"
        if table == "system_config":
            rest = sql[match.start():]
            if re.search(r"\|\||\bCONCAT\s*\(|\bLIKE\b|\bGLOB\b", rest, re.I):
                return "system_config"  # key built or pattern-matched: fail closed
            if verb in {"UPDATE", "DELETE"} and not _KEY_EQUALS.search(rest):
                # ORM flushes address the row by primary key; the before_flush hook
                # checks those objects' keys. Any other key-less write fails closed.
                if not _ORM_PK_WHERE.search(rest):
                    return "system_config"
                continue
            literal_keys = re.findall(r"'([^']*)'", rest)
            if any(_is_guarded_config_key(k) for k in literal_keys) or any(
                    _config_param_is_guarded(n, v) for n, v in _parameter_values(parameters)):
                return "system_config"
    return None


def check_credential_write(statement: str, parameters: Any) -> None:
    """Refuse a guarded write when the current request is an API-token request."""
    if not _NONINTERACTIVE.get():
        return
    target = guarded_write_target(statement, parameters)
    if target is not None:
        raise CredentialWriteRefused(target)


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    check_credential_write(statement, parameters)


def _before_flush(session, flush_context, instances):
    if not _NONINTERACTIVE.get():
        return
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if getattr(obj, "__tablename__", None) == "system_config" and _is_guarded_config_key(getattr(obj, "key", None)):
            raise CredentialWriteRefused("system_config")


def _guard_raw_dbapi_adapter() -> None:
    """core.db_utils hands out raw DBAPI cursors, which bypass SQLAlchemy events.
    Its source is pinned by migration 029, so the check is attached at runtime."""
    from core import db_utils
    adapter = db_utils._CursorAdapter
    if getattr(adapter, "_odin_credential_guard", False):
        return
    execute, executemany = adapter.execute, adapter.executemany

    def guarded_execute(self, statement, parameters=None):
        check_credential_write(statement, parameters)
        return execute(self, statement, parameters)

    def guarded_executemany(self, statement, parameters):
        check_credential_write(statement, parameters)
        return executemany(self, statement, parameters)

    adapter.execute, adapter.executemany = guarded_execute, guarded_executemany
    adapter._odin_credential_guard = True


_INSTALLED = False


def install_credential_write_guard() -> None:
    """Attach the guard to every Engine, Session and the raw DBAPI adapter (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    from sqlalchemy.orm import Session
    event.listen(Engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(Session, "before_flush", _before_flush)
    _guard_raw_dbapi_adapter()
    _INSTALLED = True
