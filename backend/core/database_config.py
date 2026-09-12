"""Database URL, secret, and engine construction for every ODIN process."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.pool import NullPool, QueuePool


class DatabaseConfigurationError(ValueError):
    """Raised when database configuration could expose secrets or is unsupported."""


APPLICATION_NAMES = {
    "api": "odin-api",
    "bootstrap": "odin-bootstrap",
    "monitor-bambu": "odin-monitor-bambu",
    "monitor-moonraker": "odin-monitor-moonraker",
    "monitor-prusalink": "odin-monitor-prusalink",
    "monitor-elegoo": "odin-monitor-elegoo",
    "vision": "odin-vision",
    "timelapse": "odin-timelapse",
    "reports": "odin-reports",
    "backup": "odin-backup",
    "restore": "odin-restore",
}

_POSTGRES_BACKENDS = frozenset({"postgresql", "postgres"})
RESTORE_ADVISORY_LOCK = 71403114720480979
_RESTORE_LOCK_EXEMPT_ROLES = frozenset({"backup", "restore"})


def _read_secret_file(path_value: str | None) -> str:
    if not path_value:
        raise DatabaseConfigurationError(
            "PostgreSQL requires DATABASE_PASSWORD_FILE; passwords in DATABASE_URL are refused"
        )
    path = Path(path_value)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise DatabaseConfigurationError("PostgreSQL password file is not readable") from exc
    if mode & 0o077:
        raise DatabaseConfigurationError(
            "PostgreSQL password file must not be readable by group or other users"
        )
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise DatabaseConfigurationError("PostgreSQL password file is not readable") from exc
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise DatabaseConfigurationError("PostgreSQL password file is invalid")
    return value


def resolve_database_url(
    database_url: str,
    *,
    password_file: str | None = None,
) -> URL:
    """Return a SQLAlchemy URL, resolving PostgreSQL password material in memory."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise DatabaseConfigurationError("DATABASE_URL is invalid") from exc

    backend = url.get_backend_name()
    if backend == "sqlite":
        return url
    if backend not in _POSTGRES_BACKENDS:
        raise DatabaseConfigurationError(
            "Unsupported database backend; use sqlite or postgresql"
        )
    if url.password is not None:
        raise DatabaseConfigurationError(
            "Credential-bearing PostgreSQL DATABASE_URL values are refused; use DATABASE_PASSWORD_FILE"
        )
    if "application_name" in url.query:
        raise DatabaseConfigurationError(
            "PostgreSQL application_name is managed by ODIN and must not be supplied in DATABASE_URL"
        )
    if not url.username or not url.database:
        raise DatabaseConfigurationError(
            "PostgreSQL DATABASE_URL must include a username and database"
        )
    password = _read_secret_file(password_file or os.getenv("DATABASE_PASSWORD_FILE"))
    return url.set(drivername="postgresql+psycopg", password=password)


def application_name(role: str) -> str:
    try:
        return APPLICATION_NAMES[role]
    except KeyError as exc:
        raise DatabaseConfigurationError(f"Unsupported ODIN database role: {role}") from exc


def create_database_engine(
    database_url: str,
    *,
    role: str = "api",
    password_file: str | None = None,
    echo: bool = False,
) -> Engine:
    """Create a dialect-correct engine without persisting a resolved credential URL."""
    url = resolve_database_url(database_url, password_file=password_file)
    if url.get_backend_name() == "sqlite":
        return create_engine(
            url,
            echo=echo,
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
    engine = create_engine(
        url,
        echo=echo,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        connect_args={"application_name": application_name(role)},
    )
    if role not in _RESTORE_LOCK_EXEMPT_ROLES:
        @event.listens_for(engine, "connect")
        def _hold_restore_barrier(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute(
                    "SELECT pg_advisory_lock_shared(%s)",
                    (RESTORE_ADVISORY_LOCK,),
                )
                cursor.execute("SET lock_timeout = 0")
            finally:
                cursor.close()
    return engine


def database_identity(database_url: str) -> dict[str, object]:
    """Return a redacted, artifact-safe database identity."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise DatabaseConfigurationError("DATABASE_URL is invalid") from exc
    dialect = url.get_backend_name()
    return {
        "dialect": "postgresql" if dialect in _POSTGRES_BACKENDS else dialect,
        "host": url.host,
        "port": url.port,
        "database": url.database,
    }
