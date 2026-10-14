"""Executable marker ownership, caller rollback and SQLite writer fences."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('kind', ['session', 'connection', 'raw', 'adapter'])
def test_scope_retains_caller_transaction_and_fences_empty_dispatch_set(review_db, kind):
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    from core.db_utils import _ConnectionAdapter
    engine = review_db.get_bind()
    raw = None
    if kind == 'session':
        executor = Session(engine); executor.begin()
    elif kind == 'connection':
        executor = engine.connect(); executor.begin()
    else:
        raw = engine.raw_connection(); raw.execute('PRAGMA foreign_keys=ON'); raw.execute('BEGIN IMMEDIATE')
        executor = _ConnectionAdapter(raw, False) if kind == 'adapter' else raw
    try:
        with start_transaction(executor) as scope:
            with pytest.raises(SourceCreationError, match='must be fenced'):
                scope.require_fenced(executor)
            scope.fence_dispatch_membership(executor)
            scope.require_fenced(executor)
            from core.economic_source_creation import _execute
            _execute(executor, "INSERT INTO system_config(key,value) VALUES('owned_start_scope','uncommitted')")
            scope.checkpoint(executor)
        with pytest.raises(SourceCreationError, match='no longer current'):
            scope.checkpoint(executor)
        (raw if raw is not None else executor).rollback()
        with engine.connect() as fresh:
            assert fresh.execute(text("SELECT count(*) FROM system_config WHERE key='owned_start_scope'")).scalar_one() == 0
    finally:
        (raw if raw is not None else executor).close()


@pytest.mark.parametrize('terminal', ['commit', 'rollback'])
def test_dbapi_commit_or_rollback_rebegin_expires_original_marker(review_db, terminal):
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    raw = review_db.get_bind().raw_connection()
    try:
        raw.execute('PRAGMA foreign_keys=ON'); raw.execute('BEGIN IMMEDIATE')
        with pytest.raises(SourceCreationError, match='marker expired'):
            with start_transaction(raw) as scope:
                scope.fence_dispatch_membership(raw)
                getattr(raw, terminal)(); raw.execute('BEGIN IMMEDIATE')
                scope.checkpoint(raw)
        assert raw.driver_connection.in_transaction
        raw.rollback()
    finally:
        raw.close()


def test_scope_exception_rolls_back_only_owned_savepoint(review_db):
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import _execute
    with Session(review_db.get_bind()) as executor:
        executor.begin()
        _execute(executor, "INSERT INTO system_config(key,value) VALUES('owned_outer','retained')")
        with pytest.raises(RuntimeError, match='owned failure'):
            with start_transaction(executor):
                _execute(executor, "INSERT INTO system_config(key,value) VALUES('owned_inner','removed')")
                raise RuntimeError('owned failure')
        assert executor.execute(text("SELECT value FROM system_config WHERE key='owned_outer'")).scalar_one() == 'retained'
        assert executor.execute(text("SELECT count(*) FROM system_config WHERE key='owned_inner'")).scalar_one() == 0
        executor.rollback()


def test_empty_dispatch_fence_takes_real_sqlite_writer_reservation(review_db):
    import sqlite3
    from core.attempt_start_transaction import start_transaction
    engine = review_db.get_bind()
    first, second = engine.raw_connection(), engine.raw_connection()
    previous_timeout = second.execute('PRAGMA busy_timeout').fetchone()[0]
    try:
        first.execute('PRAGMA foreign_keys=ON'); second.execute('PRAGMA foreign_keys=ON')
        second.execute('PRAGMA busy_timeout=0')
        first.execute('BEGIN IMMEDIATE')
        with start_transaction(first) as scope:
            scope.fence_dispatch_membership(first)
            second.execute('BEGIN')
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                with start_transaction(second) as competing:
                    competing.fence_dispatch_membership(second)
            second.rollback()
        first.rollback()
        second.execute('BEGIN IMMEDIATE')
        with start_transaction(second) as released:
            released.fence_dispatch_membership(second)
            released.require_fenced(second)
        second.rollback()
    finally:
        first.rollback(); second.rollback()
        second.execute(f'PRAGMA busy_timeout={int(previous_timeout)}')
        first.close(); second.close()


def test_education_membership_requires_explicit_scope_fence(review_db):
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    with Session(review_db.get_bind()) as executor:
        executor.begin()
        with start_transaction(executor) as scope:
            scope.fence_dispatch_membership(executor)
            with pytest.raises(SourceCreationError, match='Education membership'):
                scope.require_fenced(executor, education=True)
            scope.fence_dispatch_membership(executor, education=True)
            scope.require_fenced(executor, education=True)
        executor.rollback()


@pytest.mark.parametrize('mode', [1, 'true', None])
def test_education_fence_rejects_non_boolean_mode(review_db, mode):
    from core.attempt_start_transaction import start_transaction
    from core.economic_source_creation import SourceCreationError
    with Session(review_db.get_bind()) as executor:
        executor.begin()
        with start_transaction(executor) as scope:
            for method in (scope.fence_dispatch_membership, scope.require_fenced):
                with pytest.raises(SourceCreationError, match='Invalid Education fence mode'):
                    method(executor, education=mode)
        executor.rollback()
