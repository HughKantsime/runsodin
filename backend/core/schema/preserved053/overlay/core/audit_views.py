"""Fail-closed, cross-worker media-view windows using the existing audit ledger."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import literal_column, select, text
from sqlalchemy.orm import Session

from core.audit import log_audit
from core.audit_actor import _BINDING, _bound_postgres_actor_locks, actor_is_erased
from core.audit_actor_handoff import capture_request_actor
from core.models import AuditLog

WINDOW = timedelta(minutes=15)
CACHE_LIMIT = 4096
_cache = OrderedDict()
_cache_lock = Lock()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cached(key):
    with _cache_lock:
        expires = _cache.get(key)
        if expires is None:
            return False
        if expires <= _now():
            del _cache[key]
            return False
        _cache.move_to_end(key)
        return True


def _remember(key, recorded_at):
    # A DB hit retains its original deadline, never a sliding polling window.
    if recorded_at.tzinfo is not None:
        recorded_at = recorded_at.astimezone(timezone.utc).replace(tzinfo=None)
    with _cache_lock:
        _cache[key] = recorded_at + WINDOW
        _cache.move_to_end(key)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)


def audit_view(db, action, entity_type, entity_id, transport=None):
    """Audit an authorized read; never commit/roll back the caller's work."""
    return view_recorder(db, action, entity_type, entity_id, transport)()


def view_recorder(request_db, action, entity_type, entity_id, transport=None):
    """Capture the trusted request actor before a stream releases its session.

    Admission/revalidation owns authorization, including revocation and erasure.
    The cache only answers whether that authorized read already has a ledger row.
    """
    binder = capture_request_actor(request_db)
    engine = request_db.get_bind()

    def record():
        try:
            if binder is None:
                raise RuntimeError('Missing request audit scope')
            with Session(engine) as db, binder(db):
                scope, snapshot = db.info[_BINDING]
                if snapshot is None:
                    raise RuntimeError('Missing verified media actor')
                uid = snapshot.user_id
                window_key = (uid, scope.peer, snapshot.auth_kind,
                              action, entity_type, entity_id, transport)
                cache_key = (engine, window_key)
                if _cached(cache_key):
                    return
                actor = literal_column('audit_logs.actor_user_id')

                def recent():
                    query = select(AuditLog.timestamp).where(
                        actor == uid,
                        AuditLog.ip_address == scope.peer,
                        literal_column('audit_logs.actor_auth_kind') == snapshot.auth_kind,
                        AuditLog.action == action, AuditLog.entity_type == entity_type,
                        AuditLog.entity_id == entity_id, AuditLog.timestamp > _now() - WINDOW,
                    )
                    if transport is not None:
                        query = query.where(AuditLog.details['transport'].as_string() == transport)
                    return db.execute(query.order_by(AuditLog.timestamp.desc()).limit(1)).scalar_one_or_none()

                recorded_at = recent()
                if recorded_at is not None:
                    _remember(cache_key, recorded_at)
                    return
                db.rollback()  # fresh snapshot before serializing competing writers
                connection = db.connection()
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                elif connection.dialect.name == 'postgresql':
                    _bound_postgres_actor_locks(connection)
                    if connection.get_isolation_level().upper() != 'READ COMMITTED':
                        raise RuntimeError('View auditing requires READ COMMITTED')
                    key = json.dumps(window_key, separators=(',', ':'))
                    lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
                    connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_id})
                else:
                    raise RuntimeError('Unsupported view audit dialect')
                if uid is not None and actor_is_erased(db, uid):
                    raise RuntimeError('Media actor erased')
                recorded_at = recent()
                if recorded_at is None:
                    entry = log_audit(db, action, entity_type, entity_id,
                                      {'transport': transport} if transport else None)
                    recorded_at = entry.timestamp = _now()
                db.commit()
                _remember(cache_key, recorded_at)
        except Exception:
            raise HTTPException(503, 'View audit unavailable; try again later') from None
    return record
