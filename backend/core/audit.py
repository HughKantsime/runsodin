"""Pure caller-owned audit staging. Importing this module opens no database."""
from sqlalchemy.orm import Session
from sqlalchemy import event
from core.audit_actor import stage_actor, stamp_actor
from core.audit_integrity import capture_insert_seal, finish_insert_seal
from core.models import AuditLog


def log_audit(
    db: Session,
    action: str,
    entity_type: str = None,
    entity_id: int = None,
    details: dict = None,
    ip: str = None,
):
    """Stage an audit log entry on the session. Caller MUST commit.

    The entry is attached to ``db`` but not committed here. This is
    intentional: the audit row must be committed in the SAME transaction
    as the business mutation it describes. If callers were to commit the
    business change first and then call this, an audit insert failure
    would leave an un-audited business mutation behind — wrong error
    model, produces duplicate operations on client retry.

    Required call pattern:
        # stage business changes on db
        log_audit(db, "action", ...)
        db.commit()
    """
    from core.log_redaction import redact_structure
    if action in ('job.dispatched', 'job.dispatch_requested'):
        from core.audit_dispatch import dispatch_details
        details = dispatch_details(db, entity_id, details)
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=redact_structure(details),  # never persist a credential-bearing URL or token
        ip_address=ip,
    )
    stage_actor(db, entry, ip)
    db.add(entry)
    return entry


# No database work at import; the supplied flush Connection owns the stamp.
for listener in (capture_insert_seal, stamp_actor, finish_insert_seal):
    if not event.contains(AuditLog, "after_insert", listener):
        event.listen(AuditLog, "after_insert", listener)


def audit_download(db, action, entity_type=None, entity_id=None, details=None):
    """Commit an authorized download event before any response bytes are sent."""
    from fastapi import HTTPException
    try:
        log_audit(db, action, entity_type, entity_id, details)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, "Download audit unavailable; try again later") from None


def export_filters(**filters):
    """Keep IDs/dates; free-text filters record presence, never their contents."""
    from datetime import date
    return {key: (value.isoformat() if isinstance(value, date) else value
                  if type(value) in (int, bool) else True)
            for key, value in filters.items() if value is not None and value != ""}
