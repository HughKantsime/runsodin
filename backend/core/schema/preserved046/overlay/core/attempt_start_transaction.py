"""Private caller-owned transaction fence for prospective attempt preparation.

No observation classification, authority, attempt creation or commit occurs here.
"""
from contextlib import contextmanager
from uuid import uuid4
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from core.economic_source_creation import _connection, _execute, SourceCreationError


def _root(executor):
    return executor.get_transaction() if isinstance(executor, (Session, Connection)) else None


def _physical(connection):
    raw = connection.connection if isinstance(connection, Connection) else getattr(connection, '_connection', connection)
    return getattr(raw, 'driver_connection', raw)


class _StartTransaction:
    def __init__(self, executor):
        self._executor = executor
        self._connection = _connection(executor)
        self._physical = _physical(self._connection)
        self._root = _root(executor)
        self._marker = 'attempt_start_' + uuid4().hex
        self._active = True
        self._fenced = False
        self._start_plan_records = {}
        self._education_fenced = False
        _execute(executor, 'SAVEPOINT ' + self._marker)

    def _current(self, executor):
        if not self._active or executor is not self._executor:
            raise SourceCreationError('Attempt preparation scope is no longer current')
        connection = _connection(executor)
        if _physical(connection) is not self._physical or _root(executor) is not self._root:
            self._active = False
            raise SourceCreationError('Attempt preparation transaction changed')
        return connection

    def checkpoint(self, executor):
        """Prove the original DBAPI marker survived before a later mutation."""
        self._current(executor)
        try:
            _execute(executor, 'RELEASE SAVEPOINT ' + self._marker)
            _execute(executor, 'SAVEPOINT ' + self._marker)
        except Exception:
            self._active = False
            raise SourceCreationError('Attempt preparation transaction marker expired') from None

    def fence_dispatch_membership(self, executor, *, education=False):
        if type(education) is not bool:
            raise SourceCreationError('Invalid Education fence mode')
        self.checkpoint(executor)
        driver = self._physical
        postgres = (self._connection.dialect.name == 'postgresql'
                    if isinstance(self._connection, Connection) else hasattr(driver, 'info'))
        if postgres:
            # pg_settings supplies milliseconds regardless of SHOW formatting.
            # Subsequent factory UPDATE locks also retain this caller-local cap.
            setting = _execute(executor, "SELECT setting FROM pg_settings WHERE name='lock_timeout'").fetchone()
            try:
                milliseconds = int(setting[0])
            except (TypeError, ValueError, IndexError):
                raise SourceCreationError('Unable to prove bounded lock timeout') from None
            if milliseconds < 0:
                raise SourceCreationError('Invalid lock timeout')
            if milliseconds == 0 or milliseconds > 2000:
                _execute(executor, "SELECT set_config('lock_timeout','2000ms',true)")
            # NOWAIT rejects predicate contention immediately; a stricter
            # preexisting positive lock timeout is never weakened.
            tables = ['dispatch_attempt_receipts', 'printer_dispatch_claims']
            if education:
                tables.extend(('education_cost_centers', 'education_cost_center_printers',
                               'education_submissions', 'education_monitor_claims'))
            _execute(executor, 'LOCK TABLE ' + ', '.join('public.' + name for name in sorted(tables))
                     + ' IN SHARE MODE NOWAIT')
        else:
            # Acquire SQLite's writer reservation even for an empty predicate.
            _execute(executor, 'UPDATE printer_dispatch_claims SET token=token WHERE 0')
        self._fenced = True
        self._education_fenced = self._education_fenced or education

    def require_fenced(self, executor, *, education=False):
        if type(education) is not bool:
            raise SourceCreationError('Invalid Education fence mode')
        self.checkpoint(executor)
        if not self._fenced:
            raise SourceCreationError('Dispatch membership must be fenced before discovery')
        if education and not self._education_fenced:
            raise SourceCreationError('Education membership must be fenced before discovery')

    def _clear_start_plans(self):
        # Explicit retirement even when callers retain scope/plan after an error.
        for record in self._start_plan_records.values():
            record.clear()
            record['finalized'] = True
        self._start_plan_records.clear()

    def _finish(self, rollback):
        if not self._active:
            return
        self._current(self._executor)
        try:
            if rollback:
                _execute(self._executor, 'ROLLBACK TO SAVEPOINT ' + self._marker)
            _execute(self._executor, 'RELEASE SAVEPOINT ' + self._marker)
        finally:
            self._active = False


@contextmanager
def start_transaction(executor):
    """Require an existing transaction; caller alone commits or rolls it back."""
    scope = _StartTransaction(executor)
    try:
        yield scope
    except BaseException:
        # Expired/aborted markers must not replace the original failure. The
        # caller remains responsible for outer rollback, never reconnection.
        try:
            scope._finish(True)
        except Exception:
            scope._active = False
        raise
    else:
        scope._finish(False)
    finally:
        scope._active = False
        scope._clear_start_plans()
