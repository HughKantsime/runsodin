"""Pure caller-owned audit staging. Importing this module opens no database."""
from sqlalchemy.orm import Session
from sqlalchemy import event
from core.audit_actor import stage_actor, stamp_actor
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
if not event.contains(AuditLog, "after_insert", stamp_actor):
    event.listen(AuditLog, "after_insert", stamp_actor)
