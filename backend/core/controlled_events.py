"""Resolve event references before exposing controlled names or external copy."""
from sqlalchemy import text
from core.controlled_parts import allowed, policies, any_marks, any_read_restrictions


def references(db, data):
    refs = [(kind, data[key]) for key, kind in (
        ('job_id', 'job'), ('scheduled_job_id', 'job'), ('print_job_id', 'print_job'),
        ('printer_id', 'printer'), ('file_id', 'file'), ('model_id', 'model')) if data.get(key) is not None]
    resolved_job = False
    for kind, identifier in refs:
        if kind in ('job', 'print_job'):
            table = 'jobs' if kind == 'job' else 'print_jobs'
            projection = 'id'
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table and projection come from the closed event-kind mapping; event id is bound
            resolved_job |= db.execute(text(f'SELECT {projection} FROM {table} WHERE id=:id'), {'id': identifier}).scalar() is not None
    # A terminal event's printer may already be idle. Never infer the completed
    # work's audience from the current printer state or a caller supplied name.
    unknown = bool(data.get('job_name') or data.get('filename')) and not resolved_job
    return refs, unknown


def event_allowed(principal, message, *, db=None):
    if db is None:
        from core.db import SessionLocal
        try:
            with SessionLocal() as session:
                return event_allowed(principal, message, db=session)
        except Exception as error:
            from core.controlled_warnings import warn_failure
            warn_failure('event', error)
            return False
    if not any_read_restrictions(db):
        return True
    refs, unknown = references(db, message.get('data') or {})
    return not (unknown and any_marks(db)) and all(allowed(db, kind, identifier, principal) for kind, identifier in refs)


def external_payload(db, payload, *, job_id=None):
    if not any_read_restrictions(db):
        return payload
    # Alert job identity is policy context; it must not become a wire field.
    refs, unknown = references(db, {**payload, 'job_id': job_id} if job_id is not None else payload)
    if (unknown and any_marks(db)) or any(policies(db, kind, identifier) for kind, identifier in refs):
        # Only numeric identity and bounded event status survive. Free-form
        # metadata, URLs, filenames, reasons and nested payloads never leave.
        safe = {key: value for key, value in payload.items() if key in (
            'printer_id', 'job_id', 'print_job_id', 'scheduled_job_id', 'timestamp', 'severity', 'event', 'alert_type')}
        return {**safe, 'title': 'Controlled work', 'message': 'Controlled work status changed'}
    return payload


def external_copy(title, message, *, printer_id=None, job_id=None, db=None):
    if db is None:
        from core.db_utils import engine
        try:
            with engine.connect() as session:
                return external_copy(title, message, printer_id=printer_id, job_id=job_id, db=session)
        except Exception as error:
            from core.controlled_warnings import warn_failure
            warn_failure('notification', error)
            return 'Controlled work', 'Status changed'
    safe = external_payload(db, dict(title=title, message=message, printer_id=printer_id, job_id=job_id))
    return safe['title'], safe['message']


def audience_messages(principals, message):
    """One existence read per broadcast, no ORM session for an unmarked farm."""
    from core.db import engine
    from sqlalchemy.orm import Session
    with engine.connect() as connection:
        if not any_read_restrictions(connection):
            return [message for _ in principals]
        with Session(bind=connection) as db:
            result = []
            for principal in principals:
                data = message.get('data') or {}
                if data.get('printer_id') and message.get('type') in ('printer.state_changed', 'printer.connected', 'printer.disconnected', 'printer_status', 'printer_telemetry'):
                    from core.controlled_redaction import printer_status
                    result.append({**message, 'data': printer_status(db, data['printer_id'], principal, data)})
                elif event_allowed(principal, message, db=db):
                    result.append(message)
                else:
                    result.append(None)
            return result
