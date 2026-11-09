"""Mechanical caller-owned 029 transaction scope; no request/FD authority."""
from contextlib import contextmanager
import sqlite3
from uuid import uuid4
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from core.schema.migrator import MigrationError

_SEAL=object()

FENCE_TABLES = ('models','print_archives','print_files','project_import_checkpoints',
                'project_import_cleanup','project_import_members','project_import_operations',
                'project_import_root_generations')


def _refuse():
    raise MigrationError('Project import transaction could not be proven') from None


def _parts(executor):
    root = None
    if isinstance(executor, Session):
        root = executor.get_transaction()
        if root is None or not root.is_active or executor.get_nested_transaction() is not None:
            _refuse()
        connection = executor.connection()
    elif isinstance(executor, Connection):
        connection = executor
        root = executor.get_transaction()
        if root is None or not root.is_active or executor.in_nested_transaction():
            _refuse()
    elif type(executor) is sqlite3.Connection:
        connection = executor
    else:
        from core.db_utils import _ConnectionAdapter
        if type(executor) is not _ConnectionAdapter:
            _refuse()
        connection = executor._connection
    if isinstance(connection, Connection):
        if connection.closed or connection.invalidated:
            _refuse()
        driver = connection.connection.driver_connection
        postgres = connection.dialect.name == 'postgresql'
        if connection.dialect.name not in ('sqlite','postgresql'):
            _refuse()
    else:
        driver = getattr(connection, 'driver_connection', connection)
        postgres = hasattr(driver, 'info')
    if postgres:
        if driver.closed or driver.autocommit or int(driver.info.transaction_status) != 2:
            _refuse()
    elif not driver.in_transaction:
        _refuse()
    return connection, driver, root, postgres


def execute(executor, statement, parameters=()):
    connection, _, _, postgres = _parts(executor)
    if isinstance(connection, Connection):
        if postgres:
            from core.db_utils import _postgres_placeholders
            statement = _postgres_placeholders(statement)
        return connection.exec_driver_sql(statement, tuple(parameters))
    return executor.execute(statement, tuple(parameters))


class _ImportTransaction:
    def __init__(self, executor, seal):
        if seal is not _SEAL:_refuse()
        self.executor = executor
        self.connection, self.driver, self.root, self.postgres = _parts(executor)
        self.active = True
        self._fenced = False
        self.marker = 'project_import_' + uuid4().hex
        execute(executor, 'SAVEPOINT ' + self.marker)

    @property
    def fenced(self):return self._fenced

    def check(self, executor):
        if not self.active or executor is not self.executor:
            self.active=False;self._fenced=False;_refuse()
        try:connection, driver, root, postgres = _parts(executor)
        except BaseException:
            self.active=False;self._fenced=False;raise
        if (connection is not self.connection or driver is not self.driver or root is not self.root
                or postgres is not self.postgres):
            self.active = False
            _refuse()
        try:
            execute(executor, 'RELEASE SAVEPOINT ' + self.marker)
            execute(executor, 'SAVEPOINT ' + self.marker)
        except BaseException:
            self.active = False
            _refuse()

    def _catalog(self):
        names = ','.join("('"+name+"')" for name in FENCE_TABLES)
        rows = execute(self.executor,"SELECT n.name,to_regclass(n.name)::oid,to_regclass('public.'||n.name)::oid,c.relkind,c.relpersistence FROM (VALUES "+names+") n(name) LEFT JOIN pg_class c ON c.oid=to_regclass(n.name) ORDER BY n.name COLLATE \"C\"").fetchall()
        if len(rows) != len(FENCE_TABLES) or any(tuple(row[:1]) != (name,) or row[1] is None or row[1] != row[2] or tuple(row[3:]) != ('r','p') for name,row in zip(FENCE_TABLES,rows)):
            _refuse()
        return tuple(tuple(row) for row in rows)

    def fence(self, executor):
        self.check(executor)
        if self.postgres:
            if execute(executor,"SHOW transaction_isolation").fetchone()[0] != 'read committed':
                _refuse()
            row = execute(executor,"SELECT setting FROM pg_settings WHERE name='lock_timeout'").fetchone()
            try:
                milliseconds = int(row[0])
            except (TypeError, ValueError, IndexError):
                _refuse()
            if milliseconds < 0:
                _refuse()
            if milliseconds == 0 or milliseconds > 2000:
                execute(executor,"SELECT set_config('lock_timeout','2000ms',true)")
            before = self._catalog()
            for name in FENCE_TABLES:
                execute(executor,'LOCK TABLE public.'+name+' IN SHARE MODE NOWAIT')
            if before != self._catalog():
                _refuse()
        else:
            if self.driver.execute('PRAGMA foreign_keys').fetchone()[0] != 1 or self.driver.execute('PRAGMA ignore_check_constraints').fetchone()[0] != 0:
                _refuse()
            # Reserve the caller's existing physical writer before membership reads.
            execute(executor,'UPDATE project_import_operations SET lifecycle_revision=lifecycle_revision WHERE 0')
            placeholders=','.join('?' for _ in FENCE_TABLES)
            if execute(executor,'SELECT 1 FROM sqlite_temp_master WHERE name IN ('+placeholders+')',FENCE_TABLES).fetchone():
                _refuse()
        self._fenced = True
        self.check(executor)

    def close(self):
        if not self.active:
            return
        try:
            self.check(self.executor)
            execute(self.executor,'RELEASE SAVEPOINT '+self.marker)
        finally:
            self.active = False
            self._fenced = False


@contextmanager
def project_import_transaction(executor):
    scope = _ImportTransaction(executor,_SEAL)
    try:
        yield scope
    except BaseException:
        scope.active = False
        scope._fenced = False
        raise
    else:
        scope.close()
