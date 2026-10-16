"""Dialect-aware DBAPI access for ODIN monitor and notification daemons.

Legacy daemon modules use positional ``?`` parameters and cursor-style access.
This adapter preserves that API while sourcing connections from ODIN's central
SQLAlchemy engine, translating placeholders for psycopg, and avoiding any
fallback to ``/data/odin.db`` in PostgreSQL processes.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable

from core.db import engine


def _postgres_placeholders(statement: str) -> str:
    """Translate qmark parameters outside SQL string literals to psycopg style."""
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote:
            output.append(character)
            if character == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        else:
            output.append(character)
        index += 1
    return "".join(output)


class _CursorAdapter:
    def __init__(self, cursor: Any, postgres: bool):
        self._cursor = cursor
        self._postgres = postgres

    def execute(self, statement: str, parameters: Iterable[Any] | None = None):
        if self._postgres:
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                statement = "BEGIN"
            statement = _postgres_placeholders(statement)
        if parameters is None:
            self._cursor.execute(statement)
        else:
            self._cursor.execute(statement, parameters)
        return self

    def executemany(self, statement: str, parameters):
        if self._postgres:
            statement = _postgres_placeholders(statement)
        self._cursor.executemany(statement, parameters)
        return self

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _ConnectionAdapter:
    def __init__(self, connection: Any, postgres: bool):
        self._connection = connection
        self._postgres = postgres

    def cursor(self) -> _CursorAdapter:
        return _CursorAdapter(self._connection.cursor(), self._postgres)

    def execute(self, statement: str, parameters: Iterable[Any] | None = None):
        return self.cursor().execute(statement, parameters)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    @property
    def isolation_level(self):
        return getattr(self._connection, "isolation_level", None)

    @isolation_level.setter
    def isolation_level(self, value) -> None:
        if self._postgres and value is None:
            self._connection.autocommit = True
        else:
            self._connection.isolation_level = value

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


@contextmanager
def get_db(row_factory=None):
    """Yield a cursor-compatible connection for the configured database."""
    raw = engine.raw_connection()
    postgres = engine.dialect.name == "postgresql"
    connection = _ConnectionAdapter(raw, postgres)
    if row_factory is not None:
        if postgres:
            raw.close()
            raise ValueError("row_factory is only supported by SQLite")
        raw.driver_connection.row_factory = row_factory
    try:
        if not postgres:
            connection.execute("PRAGMA busy_timeout=10000")
        yield connection
    finally:
        raw.close()
