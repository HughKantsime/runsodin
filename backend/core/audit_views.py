"""Fail-closed, cross-worker media-view windows using the existing audit ledger."""
from datetime import datetime, timedelta, timezone
import hashlib

from fastapi import HTTPException
from sqlalchemy import literal_column, select, text
from sqlalchemy.orm import Session

from core.audit import log_audit
from core.audit_actor import _BINDING, _bound_postgres_actor_locks, actor_is_erased
from core.audit_actor_handoff import capture_request_actor
from core.models import AuditLog


def audit_view(db, action, entity_type, entity_id, transport=None):
    """Audit an authorized read; never commit/roll back the caller's work."""
    return view_recorder(db, action, entity_type, entity_id, transport)()


def view_recorder(request_db, action, entity_type, entity_id, transport=None):
    """Capture the trusted request actor before a stream releases its session."""
    binder = capture_request_actor(request_db)
    engine = request_db.get_bind()

    def record():
        try:
            if binder is None:
                raise RuntimeError('Missing request audit scope')
            with Session(engine) as db, binder(db):
                snapshot = db.info[_BINDING][1]
                if snapshot is None:
                    raise RuntimeError('Missing verified media actor')
                uid = snapshot.user_id
                # A user is one identity across JWTs and personal API tokens.
                actor = literal_column('audit_logs.actor_user_id')
                predicate = actor == uid if uid is not None else (
                    literal_column('audit_logs.actor_auth_kind') == 'legacy_global_api_key')

                def recent():
                    query = select(AuditLog.id).where(
                        predicate, AuditLog.action == action,
                        AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id,
                        AuditLog.timestamp > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15),
                    )
                    if transport is not None:
                        query = query.where(AuditLog.details['transport'].as_string() == transport)
                    return db.execute(query.limit(1)).first() is not None

                if recent():
                    return
                db.rollback()  # fresh snapshot before serializing competing writers
                connection = db.connection()
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                elif connection.dialect.name == 'postgresql':
                    _bound_postgres_actor_locks(connection)
                    if connection.get_isolation_level().upper() != 'READ COMMITTED':
                        raise RuntimeError('View auditing requires READ COMMITTED')
                    key = f'{uid}:{action}:{entity_type}:{entity_id}:{transport}'
                    lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
                    connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock_id})
                else:
                    raise RuntimeError('Unsupported view audit dialect')
                if uid is not None and actor_is_erased(db, uid):
                    raise RuntimeError('Media actor erased')
                if not recent():
                    entry = log_audit(db, action, entity_type, entity_id,
                                      {'transport': transport} if transport else None)
                    entry.timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
        except Exception:
            raise HTTPException(503, 'View audit unavailable; try again later') from None
    return record
