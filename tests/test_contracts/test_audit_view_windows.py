"""Actual ledger windows, competing worker sessions, and erasure fencing."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_audit_actor_storage import engine
from core.audit_actor import RequestAuditScope, bind_verified_actor, erase_actor_history
from core.audit_views import audit_view


def bound(engine, uid=1, kind='bearer_jwt'):
    db = Session(engine)
    RequestAuditScope('192.0.2.4').register(db)
    bind_verified_actor(db, {'id': uid, 'username': 'alice' if uid == 1 else 'admin'}, kind)
    return db


def count(engine):
    with engine.connect() as conn:
        return conn.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()


def test_windows_transport_identity_and_expiry(engine):
    with bound(engine) as db:
        for transport in ('snapshot', 'mjpeg', 'webrtc'):
            audit_view(db, 'camera.viewed', 'printer', 7, transport)
            audit_view(db, 'camera.viewed', 'printer', 7, transport)
    assert count(engine) == 3
    # Different credentials for the same user do not bypass the window.
    with bound(engine, kind='user_api_token') as db:
        audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
    assert count(engine) == 3
    with bound(engine, uid=2) as db:
        audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
    with bound(engine) as db:
        audit_view(db, 'camera.viewed', 'printer', 8, 'snapshot')
    assert count(engine) == 5
    with engine.begin() as conn:
        conn.execute(text('UPDATE audit_logs SET timestamp=:old'), {'old': datetime.utcnow()-timedelta(minutes=16)})
    with bound(engine) as db:
        audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
    assert count(engine) == 6


def test_concurrent_worker_sessions_insert_once(engine):
    barrier = Barrier(2)
    # Force both initial read checks to observe no row before either takes the lock.
    import core.audit_views as views
    def query_done(conn, cursor, statement, params, context, many):
        if statement.startswith('SELECT audit_logs.id') and not conn.info.get('checked'):
            conn.info['checked'] = True
            barrier.wait(timeout=5)
    event.listen(engine, 'after_cursor_execute', query_done)
    def worker(_):
        with bound(engine) as db:
            views.audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, range(2)))
    finally:
        event.remove(engine, 'after_cursor_execute', query_done)
    assert count(engine) == 1


def test_window_hit_never_writes(engine):
    with bound(engine) as db:
        audit_view(db, 'timelapse.viewed', 'timelapse', 9)
        def refuse_write(conn, cursor, statement, params, context, many):
            assert not statement.startswith(('INSERT', 'UPDATE', 'BEGIN IMMEDIATE'))
        event.listen(engine, 'before_cursor_execute', refuse_write)
        try:
            audit_view(db, 'timelapse.viewed', 'timelapse', 9)
        finally:
            event.remove(engine, 'before_cursor_execute', refuse_write)
    assert count(engine) == 1


def test_write_failure_and_unknown_actor_refuse(engine):
    def fail(conn, cursor, statement, params, context, many):
        if statement.startswith('INSERT INTO audit_logs'):
            raise RuntimeError('owned insertion refusal')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        with bound(engine) as db, pytest.raises(HTTPException) as error:
            audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
        assert error.value.status_code == 503
    finally:
        event.remove(engine, 'before_cursor_execute', fail)
    with Session(engine) as db, pytest.raises(HTTPException):
        audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
    assert count(engine) == 0


def test_view_erasure_scrubs_actor_and_ip(engine):
    with bound(engine) as db:
        audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
        audit_view(db, 'timelapse.viewed', 'timelapse', 8)
    with Session(engine) as db:
        erase_actor_history(db, 1)
        db.commit()
        rows = db.execute(text('SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs')).all()
        assert rows == [(None, None, None, None)] * 2
    with bound(engine) as db, pytest.raises(HTTPException):
        audit_view(db, 'camera.viewed', 'printer', 7, 'snapshot')
    assert count(engine) == 2
