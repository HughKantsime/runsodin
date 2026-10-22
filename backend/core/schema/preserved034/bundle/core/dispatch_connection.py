"""One database connection for the after-commit work of a single dispatch.

After a request commits, alert delivery resolves the alert scope, writes the WebSocket
event row (core.ws_hub) and may deliver browser push. Each of those opened its own
connection; with NullPool every one re-parses the SQLite schema. ``dispatch_scope``
opens (lazily) one connection for the whole dispatch, and each piece of work runs in
its own transaction on it through ``dispatch_transaction``, so a failure still rolls
back only that piece. The scope never wraps the request's own transaction: callers
enter it only after the request committed.

Outside a scope, ``dispatch_transaction`` opens and closes its own connection, as before.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from sqlalchemy.engine import Connection, Engine


class _Shared:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._connection: Connection | None = None

    def connection(self) -> Connection:
        if self._connection is None or self._connection.closed or self._connection.invalidated:
            if self._connection is not None:
                self._connection.close()
            self._connection = self.engine.connect()
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


_current: ContextVar[_Shared | None] = ContextVar("odin_dispatch_connection", default=None)


@contextmanager
def dispatch_scope(engine: Engine) -> Iterator[None]:
    """Share one connection among the dispatch's transactions (enter after the commit)."""
    if _current.get() is not None:  # nested dispatch: keep the outer connection
        yield
        return
    shared = _Shared(engine)
    token = _current.set(shared)
    try:
        yield
    finally:
        _current.reset(token)
        shared.close()


@contextmanager
def dispatch_transaction(engine: Engine) -> Iterator[Connection]:
    """A transaction of its own: on the dispatch's shared connection, else a fresh one."""
    shared = _current.get()
    if shared is None or shared.engine.url != engine.url:
        with engine.begin() as connection:
            yield connection
        return
    connection = shared.connection()
    if connection.in_transaction():
        connection.rollback()  # never inherit another piece's open transaction
    with connection.begin():
        yield connection
